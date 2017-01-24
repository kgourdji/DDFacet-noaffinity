'''
DDFacet, a facet-based radio imaging package
Copyright (C) 2013-2016  Cyril Tasse, l'Observatoire de Paris,
SKA South Africa, Rhodes University

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
'''

import psutil
import os
import fnmatch
import Queue
import multiprocessing
import numpy as np
import traceback
import inspect
from collections import OrderedDict

from DDFacet.Other import MyLogger
from DDFacet.Other import ClassTimeIt
from DDFacet.Other import ModColor
from DDFacet.Other.progressbar import ProgressBar
from DDFacet.Array import SharedDict

log = MyLogger.getLogger("AsyncProcessPool")

# PID of parent process
parent_pid = os.getpid()

class Job(object):
    def __init__ (self, job_id, jobitem, singleton=False, event=None, when_complete=None):
        self.job_id, self.jobitem, self.singleton, self.event, self.when_complete = \
            job_id, jobitem, singleton, event, (when_complete or (lambda:None))
        self.result = None
        self.complete = False

    def setResult (self, result):
        self.result = result
        self.complete = True
        self.when_complete()

class JobCounterPool(object):
    """Implements a condition variable that is a counter. Typically used to keep track of the number of pending jobs
    of a particular type, and to block until all are complete"""

    class JobCounter(object):
        def __init__ (self, pool, name=None):
            self.name = name or "%x"%id(self)
            self._cond = multiprocessing.Condition()
            self._pool = pool
            pool._register(self)

        def increment(self):
            """Increments the counter"""
            with self._cond:  # acquire lock
                self._pool._counters_array[self.index_in_pool] += 1

        def decrement(self):
            """Decrements the named counter. When it gets to zero, notifies any waiting processes."""
            with self._cond:  # acquire lock
                self._pool._counters_array[self.index_in_pool] -= 1
                # if decremented to 0, notify callers
                if self._pool._counters_array[self.index_in_pool] <= 0:
                    self._cond.notify_all()

        def getValue(self):
            with self._cond:
                return self._pool._counters_array[self.index_in_pool]

        def awaitZero(self):
            with self._cond:  # acquire lock
                while self._pool._counters_array[self.index_in_pool] != 0:
                    self._cond.wait()
            return 0

        def awaitZeroWithTimeout(self, timeout):
            with self._cond:  # acquire lock
                if not self._pool._counters_array[self.index_in_pool]:
                    return 0
                self._cond.wait(timeout)
                return self._pool._counters_array[self.index_in_pool]

    def __init__(self):
        self._counters = OrderedDict()
        self._counters_array = None

    def new(self, name=None):
        """Creates a new counter and registers this in this pool"""
        return JobCounterPool.JobCounter(self, name)

    def get(self, counter_id):
        """Returns counter object corresponding to ID"""
        return self._counters[counter_id]

    def finalize(self, shared_dict):
        """Called in parent process to complete initialization of all counters"""
        if os.getpid() != parent_pid:
            raise RuntimeError("This method can only be called in the parent process. This is a bug.")
        if self._counters_array is not None:
            raise RuntimeError("Workers already started. This is a bug.")
        self._counters_array = shared_dict.addSharedArray("Counters", (len(self._counters),), np.int32)

    def _register(self, counter):
        cid = id(counter)
        if cid in self._counters:
            raise RuntimeError,"job counter %s already exists. This is a bug."%cid
        if os.getpid() != parent_pid:
            raise RuntimeError("This method can only be called in the parent process. This is a bug.")
        if self._counters_array is not None:
            raise RuntimeError("Workers already started. This is a bug.")
        counter.index_in_pool = len(self._counters)
        self._counters[cid] = counter


class AsyncProcessPool (object):
    """
    """
    def __init__ (self):
        self._started = False

    def __del__(self):
        self.shutdown()

    def init(self, ncpu=None, affinity=None, num_io_processes=1, verbose=0):
        """
        Initializes an APP.

        Args:
            ncpu:
            affinity:
            num_io_processes:
            verbose:

        Returns:

        """
        self._shared_state = SharedDict.create("APP")
        self.affinity = affinity
        self.cpustep = abs(self.affinity) or 1
        self.ncpu = ncpu
        self.verbose = verbose
        maxcpu = psutil.cpu_count() / self.cpustep
        # if NCPU is 0, set to number of CPUs on system
        if not self.ncpu:
            self.ncpu = maxcpu
        elif self.ncpu > maxcpu:
            print>>log,ModColor.Str("NCPU=%d is too high for this setup (%d cores, affinity %d)" %
                                    (self.ncpu, psutil.cpu_count(), self.affinity))
            print>>log,ModColor.Str("Falling back to NCPU=%d" % (maxcpu))
            self.ncpu = maxcpu
        self.procinfo = psutil.Process()  # this will be used to control CPU affinity

        # create a queue for compute-bound tasks
        # generate list of CPU cores for workers to run on
        if not self.affinity or self.affinity == 1:
            cores = range(self.ncpu)
        elif self.affinity == 2:
            cores = range(0, self.ncpu*2, 2)
        elif self.affinity == -2:
            cores = range(0, self.ncpu*2, 4) + range(1, self.ncpu*2, 4)
        else:
            raise ValueError,"unknown affinity setting %d" % self.affinity
        self._compute_workers = []
        self._io_workers = []
        self._compute_queue   = multiprocessing.Queue()
        self._io_queues       = [ multiprocessing.Queue() for x in xrange(num_io_processes) ]
        self._result_queue    = multiprocessing.Queue()
        self._job_handlers = {}
        self._events = {}
        self._results_map = {}
        self._job_counters = JobCounterPool()

        # create the workers
        for i, core in enumerate(cores):
            proc_id = "comp%02d"%i
            self._compute_workers.append( multiprocessing.Process(target=self._start_worker, args=(self, proc_id, [core], self._compute_queue)) )
        for i, queue in enumerate(self._io_queues):
            proc_id = "io%02d"%i
            self._io_workers.append( multiprocessing.Process(target=self._start_worker, args=(self, proc_id, None, queue)) )
        self._started = False

    def registerJobHandlers (self, *handlers):
        """Adds recognized job handlers. Job handlers may be functions or objects."""
        if os.getpid() != parent_pid:
            raise RuntimeError("This method can only be called in the parent process. This is a bug.")
        if self._started:
            raise RuntimeError("Workers already started. This is a bug.")
        for handler in handlers:
            if not inspect.isfunction(handler) and not isinstance(handler, object):
                raise RuntimeError("Job handler must be a function or object. This is a bug.")
            self._job_handlers[id(handler)] = handler

    def registerEvents (self, *args):
        if os.getpid() != parent_pid:
            raise RuntimeError("This method can only be called in the parent process. This is a bug.")
        if self._started:
            raise RuntimeError("Workers already started. This is a bug.")
        self._events.update(dict([(name,multiprocessing.Event()) for name in args]))

    def createJobCounter (self, name=None):
        if os.getpid() != parent_pid:
            raise RuntimeError("This method can only be called in the parent process. This is a bug.")
        if self._started:
            raise RuntimeError("Workers already started. This is a bug.")
        return self._job_counters.new(name)

    def startWorkers(self):
        """Starts worker threads. All job handlers and events must be registered *BEFORE*"""
        self._job_counters.finalize(self._shared_state)
        for proc in self._compute_workers + self._io_workers:
            proc.start()
        self._started = True

    def runJob (self, job_id, handler=None, io=None, args=(), kwargs={},
                event=None, counter=None,
                singleton=False, collect_result=True,
                serial=False):
        """
        Puts a job on a processing queue.

        Args:
            job_id:  string job identifier
            handler: function previously registered with registerJobHandler, or bound method of object that was registered.
            io:     if None, job is placed on compute queues. If 0/1/..., job is placed on an I/O queue of the given level
            event:  if not None, then the named event will be raised when the job is complete.
                    Otherwise, the job is a singleton, handled via the events directory.
            counter: if set to a JobCounter object, the job will be associated with a job counter, which will be incremented upon runJob(),
                    and decremented when the job is complete.
            collect_result: if True, job's result will be collected and returned via awaitJobResults().
                    This mode is only available in the parent process.
            singleton: if True, then job is a one-off. If collect_result=True, then when complete, its result will remain
                    in the results map forever, so that subsequent calls to awaitJobResults() on it return that result.
                    A singleton job can't be run again.
                    If False, job result will be collected by awaitJobResults() and removed from the map: the job can be
                    run again.
            serial: if True, job is run serially in the main process. Useful for debugging.
        """
        if collect_result and os.getpid() != parent_pid:
            raise RuntimeError("runJob() with collect_result can only be called in the parent process. This is a bug.")
        if collect_result and job_id in self._results_map:
            raise RuntimeError("Job '%s' has an uncollected result, or is a singleton. This is a bug."%job_id)
        # figure out the handler, and how to pass it to the queue
        # If this is a function, then describe is by function id, None
        if inspect.isfunction(handler):
            handler_id, method = id(handler), None
            handler_desc  = "%s()" % handler.__name__
        # If this is a bound method, describe it by instance id, method_name
        elif inspect.ismethod(handler):
            instance = handler.im_self
            if instance is None:
                raise RuntimeError("Job '%s': handler %s is not a bound method. This is a bug." % (job_id, handler))
            handler_id, method = id(instance), handler.__name__
            handler_desc = "%s.%s()" % (handler.im_class.__name__, method)
        else:
            raise TypeError("'handler' argument must be a function or a bound method")
        if handler_id not in self._job_handlers:
            raise RuntimeError("Job '%s': unregistered handler %s. This is a bug." % (job_id, handler))
        # resolve event object
        if event:
            eventobj = self._events[event]
            eventobj.clear()
        else:
            eventobj = None
        # increment counter object
        if counter:
            counter.increment()
        jobitem = dict(job_id=job_id, handler=(handler_id, method, handler_desc), event=event,
                       counter=counter and id(counter),
                       collect_result=collect_result,
                       args=args, kwargs=kwargs)
        if self.verbose > 2:
            print>>log, "enqueueing job %s: %s"%(job_id, function)
        # place it on appropriate queue
        if io is None:
            self._compute_queue.put(jobitem)
        else:
            io = max(len(self._io_queues)-1, io)
            self._io_queues[io].put(jobitem)
        # insert entry into dict of pending jobs
        if collect_result:
            self._results_map[job_id] = Job(job_id, jobitem, singleton=singleton)

    def awaitJobCounter (self, counter, progress=None, total=None, timeout=10):
        if self.verbose > 2:
            print>> log, "  %s is complete" % counter.name
        if progress:
            current = counter.getValue()
            total = total or current or 1
            pBAR = ProgressBar('white', width=50, block='=', empty=' ',Title="  "+progress, HeaderSize=10, TitleSize=13)
            pBAR.render(int(100.*(total-current)/total), '%4i/%i' % (total-current, total))
            while current:
                current = counter.awaitZeroWithTimeout(timeout)
                pBAR.render(int(100. * (total - current) / total), '%4i/%i' % (total - current, total))
        else:
            counter.awaitZero()
            if self.verbose > 2:
                print>> log, "  %s is complete" % counter.name

    def awaitEvents (self, *events):
        """
        Waits for events indicated by the given names to be set. This can be called from the parent process, or
        from any of the background processes.
        """
        if self.verbose > 2:
            print>>log, "checking for completion events on %s" % " ".join(events)
        for name in events:
            event = self._events.get(name)
            if event is None:
                raise KeyError("Unknown event '%s'" % name)
            while not event.is_set():
                if self.verbose > 2:
                    print>> log, "  %s not yet complete, waiting" % name
                event.wait()
            if self.verbose > 2:
                print>> log, "  %s is complete" % name

    def awaitJobResults (self, jobspecs, progress=None):
        """
        Waits for job(s) given by arguments to complete, and returns their results.
        Note that this only works for jobs scheduled by the same process, since each process has its own results map.
        A process will block indefinitely is asked to await on jobs scheduled by another process.

        Args:
            jobspec: a job spec, or a list of job specs. Each spec can contain a wildcard e.g. "job*", to wait for
            multiple jobs.

        Returns:
            a list of results. Each entry is the result returned by the job (if no wildcard), or a list
            of results from each job (if has a wildcard)
        """
        if os.getpid() != parent_pid:
            raise RuntimeError("This method can only be called in the parent process. This is a bug.")
        if type(jobspecs) is str:
            jobspecs = [ jobspecs ]
        # make a dict of all jobs still outstanding
        awaiting_jobs = {}  # this maps job_id to a set of jobspecs (if multiple) that it matches
        job_results = OrderedDict()   # this maps jobspec to a list of results
        total_jobs = complete_jobs = 0
        for jobspec in jobspecs:
            matching_jobs = [job_id for job_id in self._results_map.iterkeys() if fnmatch.fnmatch(job_id, jobspec)]
            for job_id in matching_jobs:
                awaiting_jobs.setdefault(job_id, set()).add(jobspec)
            total_jobs += len(matching_jobs)
            job_results[jobspec] = len(matching_jobs), []
        # check dict of already returned results (perhaps from previous calls to awaitJobs). Remove
        # matching results, and assign them to appropriate jobspec lists
        for job_id, job in self._results_map.items():
            if job_id in awaiting_jobs and job.complete:
                for jobspec in awaiting_jobs[job_id]:
                    job_results[jobspec][1].append(job.result)
                    complete_jobs += 1
                if not job.singleton:
                    del self._results_map[job_id]
                del awaiting_jobs[job_id]
        if progress:
            pBAR = ProgressBar('white', width=50, block='=', empty=' ',Title="  "+progress, HeaderSize=10, TitleSize=13)
            pBAR.render(int(100.*complete_jobs/total_jobs), '%4i/%i' % (complete_jobs, total_jobs))
        if self.verbose > 1:
            print>>log, "checking job results: %s (%d still pending)"%(
                ", ".join(["%s %d/%d"%(jobspec, len(results), njobs) for jobspec, (njobs, results) in job_results.iteritems()]),
                len(awaiting_jobs))
        # sit here while any pending jobs remain
        while awaiting_jobs:
            try:
                result = self._result_queue.get(True, 10)
            except Queue.Empty:
                # print>> log, "checking for dead workers"
                # shoot the zombie process, if any
                multiprocessing.active_children()
                # check for dead workers
                pids_to_restart = []
                for w in self._compute_workers + self._io_workers:
                    if not w.is_alive():
                        pids_to_restart.append(w)
                        raise RuntimeError("a worker process has died on us \
                            with exit code %d. This is probably a bug." % w.exitcode)
                continue
            # ok, dispatch the result
            job_id = result["job_id"]
            job = self._results_map.get(job_id)
            if job is None:
                raise KeyError("Job '%s' was not enqueued. This is a logic error." % job_id)
            job.setResult(result)
            # if being awaited, dispatch appropriately
            if job_id in awaiting_jobs:
                for jobspec in awaiting_jobs[job_id]:
                    job_results[jobspec][1].append(result)
                    complete_jobs += 1
                if not job.singleton:
                    del self._results_map[job_id]
                del awaiting_jobs[job_id]
                if progress:
                    pBAR.render(int(100.*complete_jobs/total_jobs), '%4i/%i' % (complete_jobs, total_jobs))
            # print status update
            if self.verbose > 1:
                print>>log,"received job results %s" % " ".join(["%s:%d"%(jobspec, len(results)) for jobspec, (_, results)
                                                             in job_results.iteritems()])
        # render complete
        if progress:
            pBAR.render(int(100. * complete_jobs / total_jobs), '%4i/%i' % (complete_jobs, total_jobs))
        # process list of results for each jobspec to check for errors
        for jobspec, (njobs, results) in job_results.iteritems():
            times = np.array([ res['time'] for res in results ])
            num_errors = len([res for res in results if not res['success']])
            if progress:
                print>> log, "%s: %d jobs complete, average single-core time %.2fs per job" % (progress, len(results), times.mean())
            elif self.verbose > 0:
                print>> log, "%s: %d jobs complete, average single-core time %.2fs per job" % (jobspec, len(results), times.mean())
            if num_errors:
                print>>log, ModColor.Str("%s: %d jobs returned an error. Aborting."%(jobspec, num_errors), col="red")
                raise RuntimeError("some distributed jobs have failed")
        # return list of results
        result_values = []
        for jobspec, (_, results) in job_results.iteritems():
            resvals = [resitem["result"] if resitem["success"] else resitem["error"] for resitem in results]
            if '*' not in jobspec:
                resvals = resvals[0]
            result_values.append(resvals)
        return result_values[0] if len(result_values) == 1 else result_values

    def terminate(self):
        if self._started:
            if self.verbose > 1:
                print>> log, "terminating workers"
            for p in self._compute_workers + self._io_workers:
                p.terminate()

    def shutdown(self):
        """Terminate worker threads"""
        if not self._started:
            return
        if self.verbose > 1:
            print>>log,"shutdown: handing poison pills to workers"
        self._started = False
        for _ in self._compute_workers:
            self._compute_queue.put("POISON-E")
        for queue in self._io_queues:
            queue.put("POISON-E")
        if self.verbose > 1:
            print>> log, "shutdown: reaping workers"
        # join processes
        for p in self._compute_workers + self._io_workers:
            p.join()
        if self.verbose > 1:
            print>> log, "shutdown: closing queues"
        # join and close queues
        self._result_queue.close()
        self._compute_queue.close()
        for queue in self._io_queues:
            queue.close()
        if self.verbose > 1:
            print>> log, "shutdown complete"

    @staticmethod
    def _start_worker (object, proc_id, affinity, worker_queue):
        """
            Helper method for worker process startup. ets up affinity, and calls _run_worker method on
            object with the specified work queue.

        Args:
            object:
            proc_id:
            affinity:
            work_queue:

        Returns:

        """
        AsyncProcessPool.proc_id = proc_id
        MyLogger.subprocess_id = proc_id
        if affinity:
            psutil.Process().cpu_affinity(affinity)
        object._run_worker(worker_queue)

    def _run_worker (self, queue):
        """
            Runs worker loop on given queue. Waits on queue, picks off job items, looks them up in context table,
            calls them, and returns results in the work queue.
        """
        if self.verbose > 0:
            print>>log,ModColor.Str("started worker pid %d"%os.getpid())
        try:
            pill = True
            # While no poisoned pill has been given grab items from the queue.
            while pill:
                try:
                    # Get queue item, or timeout and check if pill perscribed.
                    # print>>log,"%s: calling queue.get()"%AsyncProcessPool.proc_id
                    jobitem = queue.get(True, 10)
                    # print>>log,"%s: queue.get() returns %s"%(AsyncProcessPool.proc_id, jobitem)
                except Queue.Empty:
                    continue
                timer = ClassTimeIt.ClassTimeIt()
                if jobitem == "POISON-E":
                    break
                elif jobitem is not None:
                    event = counter = None
                    try:
                        job_id, eventname, counter_id, args, kwargs = [jobitem.get(attr) for attr in
                                                                    "job_id", "event", "counter", "args", "kwargs"]
                        handler_id, method, handler_desc = jobitem["handler"]
                        handler = self._job_handlers.get(handler_id)
                        if handler is None:
                            raise RuntimeError("Job %s: unknown handler %s. This is a bug." % (job_id, handler_desc))
                        event = self._events[eventname] if eventname else None
                        # find counter object, if specified
                        if counter_id:
                            counter = self._job_counters.get(counter_id)
                            if counter is None:
                                raise RuntimeError("Job %s: unknown counter %s. This is a bug." % (job_id, counter_id))
                        # call the job
                        if self.verbose > 1:
                            print>> log, "job %s: calling %s" % (job_id, handler_desc)
                        if method is None:
                            # call object directly
                            result = handler(*args, **kwargs)
                        else:
                            call = getattr(handler, method, None)
                            if not callable(call):
                                raise KeyError("Job %s: unknown method '%s' for handler %s"%(job_id, method, handler_desc))
                            result = call(*args, **kwargs)
                        if self.verbose > 3:
                            print>> log, "job %s: %s returns %s" % (job_id, handler_desc, result)
                        # Send result back
                        if jobitem['collect_result']:
                            self._result_queue.put(dict(job_id=job_id, proc_id=self.proc_id, success=True, result=result, time=timer.seconds()))
                    except KeyboardInterrupt:
                        raise
                    except Exception, exc:
                        print>> log, ModColor.Str("process %s: exception raised processing job %s: %s" % (
                            AsyncProcessPool.proc_id, job_id, traceback.format_exc()))
                        if jobitem['collect_result']:
                            self._result_queue.put(dict(job_id=job_id, proc_id=self.proc_id, success=False, error=exc, time=timer.seconds()))
                    finally:
                        # Raise event
                        if event is not None:
                            event.set()
                        if counter is not None:
                            counter.decrement()

        except KeyboardInterrupt:
            print>>log, ModColor.Str("Ctrl+C caught, exiting", col="red")
            return
    # CPU id. This will be None in the parent process, and a unique number in each worker process
    proc_id = None

APP = AsyncProcessPool()

def init(ncpu=None, affinity=None, num_io_processes=1, verbose=0):
    global APP
    APP.init(ncpu, affinity, num_io_processes, verbose)

