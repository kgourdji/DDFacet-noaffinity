'''
DDFacet, a facet-based radio imaging package
Copyright (C) 2013-2016  Cyril Tasse, l'Observatoire de Paris,
SKA-SA, Rhodes University

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

import numpy as np

class ClassSpectralFunctions():
    def __init__(self,DicoMappingDesc,BeamEnable=True):

        self.DicoMappingDesc=DicoMappingDesc
        self.NFreqBand=len(self.DicoMappingDesc["freqs"])
        self.BeamEnable=BeamEnable
        self.setFreqs()
        if self.BeamEnable:
            self.setBeamFactors()
    
    def setFreqs(self):
        AllFreqs=[]
        AllFreqsMean=np.zeros((self.NFreqBand,),np.float32)
        for iChannel in range(self.NFreqBand):
            AllFreqs+=self.DicoMappingDesc["freqs"][iChannel]
            AllFreqsMean[iChannel]=np.mean(self.DicoMappingDesc["freqs"][iChannel])

        RefFreq=np.sum(AllFreqsMean.ravel()*self.DicoMappingDesc["WeightChansImages"].ravel())

        self.AllFreqs=AllFreqs
        self.RefFreq=RefFreq

    def setBeamFactors(self):
        self.DicoBeamFactors={}
        self.NFacets=len(self.DicoMappingDesc["SumJonesChan"])
        for iFacet in range(self.NFacets):
            ListBeamFactor,ListBeamFactorWeightSq=self.GiveBeamFactorsFacet(iFacet)
            self.DicoBeamFactors[iFacet]={}
            self.DicoBeamFactors[iFacet]["ListBeamFactor"]=ListBeamFactor
            self.DicoBeamFactors[iFacet]["ListBeamFactorWeightSq"]=ListBeamFactorWeightSq


    def GiveBeamFactorsFacet(self,iFacet):
        

        SumJonesChan=self.DicoMappingDesc["SumJonesChan"][iFacet]
        SumJonesChanWeightSq=self.DicoMappingDesc["SumJonesChanWeightSq"][iFacet]
        ChanMappingGrid=self.DicoMappingDesc["ChanMappingGrid"]
        
        ListBeamFactor=[]
        ListBeamFactorWeightSq=[]

        for iChannel in range(self.NFreqBand):
            ThisSumJonesChan=[]
            ThisSumJonesChanWeightSq=[]
            for iMS in range(len(SumJonesChan)):
                ind=np.where(ChanMappingGrid[iMS]==iChannel)[0]
                ThisSumJonesChan+=SumJonesChan[iMS][ind].tolist()
                ThisSumJonesChanWeightSq+=SumJonesChanWeightSq[iMS][ind].tolist()
            ListBeamFactor.append(np.array(ThisSumJonesChan))
            ListBeamFactorWeightSq.append(np.array(ThisSumJonesChanWeightSq))

        return ListBeamFactor,ListBeamFactorWeightSq
        


    def GiveFreqBandsFluxRatio(self,iFacet,Alpha):

        NFreqBand=self.NFreqBand
        NAlpha=Alpha.size
        FreqBandsFluxRatio=np.zeros((NAlpha,NFreqBand),np.float32)

        for iChannel in range(NFreqBand):
            for iAlpha in range(NAlpha):
                ThisAlpha=Alpha[iAlpha]
                
                FreqBandsFluxRatio[iAlpha,iChannel]=self.IntExpFunc(Alpha=ThisAlpha,iChannel=iChannel,iFacet=iFacet)


        return FreqBandsFluxRatio
        
    def IntExpFunc(self,S0=1.,Alpha=0.,iChannel=0,iFacet=0):
        
        RefFreq=self.RefFreq

        ThisAlpha=Alpha
        ThisFreqs=np.array(self.DicoMappingDesc["freqs"][iChannel])
        
        S0=np.array(S0)
        Npix=S0.size

        if self.BeamEnable:
            ListBeamFactor,ListBeamFactorWeightSq=self.DicoBeamFactors[iFacet]["ListBeamFactor"],self.DicoBeamFactors[iFacet]["ListBeamFactorWeightSq"]
            BeamFactor=ListBeamFactor[iChannel].reshape((1,ThisFreqs.size))
            BeamFactorWeightSq=ListBeamFactorWeightSq[iChannel].reshape((1,ThisFreqs.size))
            MeanJonesBand=self.DicoMappingDesc["MeanJonesBand"][iFacet][iChannel]
        else:
            BeamFactor=1.
            BeamFactorWeightSq=1.
            MeanJonesBand=1.

        ThisFreqs=ThisFreqs.reshape((1,ThisFreqs.size))
        ThisAlpha=ThisAlpha.reshape((Npix,1))
        FreqBandsFlux=np.sqrt(np.sum(BeamFactor*((ThisFreqs/RefFreq)**ThisAlpha)**2,axis=1))/np.sqrt(np.sum(BeamFactorWeightSq))
        FreqBandsFlux/=np.sqrt(MeanJonesBand)

        S0=S0.reshape((Npix,))
        FreqBandsFlux*=S0



        return FreqBandsFlux.ravel()
