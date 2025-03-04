import numpy as np
import os
import math
import scipy
import scipy.io
import scipy.signal
import random
# import librosa # for data generation, test which one (scipy.signal.resample_poly, librosa.resample) is faster
import soundfile
import pandas
import warnings
import copy
from collections import namedtuple
from torch.utils.data import Dataset
import matplotlib.pyplot as plt
# from matplotlib import animation
import webrtcvad
import gpuRIR
from utils import load_file

# %% Util functions

def acoustic_power(s):
	""" Acoustic power of after removing the silences.
	"""
	w = 512  # Window size for silent detection
	o = 256  # Window step for silent detection

	# Window the input signal
	s = np.ascontiguousarray(s)
	sh = (s.size - w + 1, w)
	st = s.strides * 2
	S = np.lib.stride_tricks.as_strided(s, strides=st, shape=sh)[0::o]

	window_power = np.mean(S ** 2, axis=-1)
	th = 0.01 * window_power.max()  # Threshold for silent detection
	return np.mean(window_power[np.nonzero(window_power > th)])


def cart2sph(cart):
	xy2 = cart[:,0]**2 + cart[:,1]**2
	sph = np.zeros_like(cart)
	sph[:,0] = np.sqrt(xy2 + cart[:,2]**2)
	sph[:,1] = np.arctan2(np.sqrt(xy2), cart[:,2]) # Elevation angle defined from Z-axis down
	sph[:,2] = np.arctan2(cart[:,1], cart[:,0])
	return sph


# %% Util classes

class Parameter:
	""" Random parammeter class.
	"""
	def __init__(self, *args, discrete=False):
		self.discrete = discrete
		if discrete == False:
			if len(args) == 1:
				self.random = False
				self.value = np.array(args[0])
				self.min_value = None
				self.max_value = None
			elif len(args) == 2:
				self.random = True
				self.min_value = np.array(args[0])
				self.max_value = np.array(args[1])
				self.value = None
			else:
				raise Exception('Parammeter must be called with one (value) or two (min and max value) array_like parammeters')
		else:
			self.value_range = args[0]

	def getValue(self):
		if self.discrete == False:
			if self.random:
				return self.min_value + np.random.random(self.min_value.shape) * (self.max_value - self.min_value)
			else:
				return self.value
		else:
			idx = np.random.randint(0, len(self.value_range))
			return self.value_range[idx]


ArraySetup = namedtuple('ArraySetup', 'arrayType, orV, mic_scale, mic_pos, mic_orV, mic_pattern')

# Named tuple with the characteristics of a microphone array and definitions of dual-channel array
dualch_array_setup = ArraySetup(arrayType='planar', 
    orV = np.array([0.0, 1.0, 0.0]), # ??? put the source in oneside (indicated by orV) of the array
	mic_scale = Parameter(1), # !!! half of the mic_distance should be smaller than the minimum separation between the array and the walls defined by array_pos
	mic_pos = np.array(((
			(0.04, 0.0, 0.0),
			(-0.04, 0.0, 0.0),
	))), # Actural position is mic_scale*mic_pos
	mic_orV = None, # Invalid for omnidirectional microphones
	mic_pattern = 'omni'
)

# Named tuple with the characteristics of a microphone array and definitions of the LOCATA arrays

dicit_array_setup = ArraySetup(arrayType='planar',
    orV = np.array([0.0, 1.0, 0.0]),
	mic_scale = Parameter(1),
    mic_pos = np.array((( 0.96, 0.00, 0.00),
						( 0.64, 0.00, 0.00),
						( 0.32, 0.00, 0.00),
						( 0.16, 0.00, 0.00),
						( 0.08, 0.00, 0.00),
						( 0.04, 0.00, 0.00),
						( 0.00, 0.00, 0.00),
						( 0.96, 0.00, 0.32),
						(-0.04, 0.00, 0.00),
						(-0.08, 0.00, 0.00),
						(-0.16, 0.00, 0.00),
						(-0.32, 0.00, 0.00),
						(-0.64, 0.00, 0.00),
						(-0.96, 0.00, 0.00),
						(-0.96, 0.00, 0.32))),
    mic_orV = np.tile(np.array([[0.0, 1.0, 0.0]]), (15,1)),
    mic_pattern = 'omni'
)

dummy_array_setup = ArraySetup(arrayType='planar',
    orV = np.array([0.0, 1.0, 0.0]),
	mic_scale = Parameter(1),
    mic_pos = np.array(((-0.079,  0.000, 0.000),
						(-0.079, -0.009, 0.000),
						( 0.079,  0.000, 0.000),
						( 0.079, -0.009, 0.000))),
    mic_orV = np.array(((-1.0, 0.0, 0.0),
						(-1.0, 0.0, 0.0),
						( 1.0, 0.0, 0.0),
						( 1.0, 0.0, 0.0))),
    mic_pattern = 'omni'
)

benchmark2_array_setup = ArraySetup(arrayType='3D',
    orV = np.array([0.0, 1.0, 0.0]),
	mic_scale = Parameter(1),
    mic_pos = np.array(((-0.028,  0.030, -0.040),
						( 0.006,  0.057,  0.000),
						( 0.022,  0.022, -0.046),
						(-0.055, -0.024, -0.025),
						(-0.031,  0.023,  0.042),
						(-0.032,  0.011,  0.046),
						(-0.025, -0.003,  0.051),
						(-0.036, -0.027,  0.038),
						(-0.035, -0.043,  0.025),
						( 0.029, -0.048, -0.012),
						( 0.034, -0.030,  0.037),
						( 0.035,  0.025,  0.039))),
    mic_orV = np.array(((-0.028,  0.030, -0.040),
						( 0.006,  0.057,  0.000),
						( 0.022,  0.022, -0.046),
						(-0.055, -0.024, -0.025),
						(-0.031,  0.023,  0.042),
						(-0.032,  0.011,  0.046),
						(-0.025, -0.003,  0.051),
						(-0.036, -0.027,  0.038),
						(-0.035, -0.043,  0.025),
						( 0.029, -0.048, -0.012),
						( 0.034, -0.030,  0.037),
						( 0.035,  0.025,  0.039))),
    mic_pattern = 'omni'
)

eigenmike_array_setup = ArraySetup(arrayType='3D',
    orV = np.array([0.0, 1.0, 0.0]),
	mic_scale = Parameter(1),
    mic_pos = np.array((( 0.000,  0.039,  0.015),
						(-0.022,  0.036,  0.000),
						( 0.000,  0.039, -0.015),
						( 0.022,  0.036,  0.000),
						( 0.000,  0.022,  0.036),
						(-0.024,  0.024,  0.024),
						(-0.039,  0.015,  0.000),
						(-0.024,  0.024,  0.024),
						( 0.000,  0.022, -0.036),
						( 0.024,  0.024, -0.024),
						( 0.039,  0.015,  0.000),
						( 0.024,  0.024,  0.024),
						(-0.015,  0.000,  0.039),
						(-0.036,  0.000,  0.022),
						(-0.036,  0.000, -0.022),
						(-0.015,  0.000, -0.039),
						( 0.000, -0.039,  0.015),
						( 0.022, -0.036,  0.000),
						( 0.000, -0.039, -0.015),
						(-0.022, -0.036,  0.000),
						( 0.000, -0.022,  0.036),
						( 0.024, -0.024,  0.024),
						( 0.039, -0.015,  0.000),
						( 0.024, -0.024, -0.024),
						( 0.000, -0.022, -0.036),
						(-0.024, -0.024, -0.024),
						(-0.039, -0.015,  0.000),
						(-0.024, -0.024,  0.024),
						( 0.015,  0.000,  0.039),
						( 0.036,  0.000,  0.022),
						( 0.036,  0.000, -0.022),
						( 0.015,  0.000, -0.039))),
    mic_orV = np.array((( 0.000,  0.039,  0.015),
						(-0.022,  0.036,  0.000),
						( 0.000,  0.039, -0.015),
						( 0.022,  0.036,  0.000),
						( 0.000,  0.022,  0.036),
						(-0.024,  0.024,  0.024),
						(-0.039,  0.015,  0.000),
						(-0.024,  0.024,  0.024),
						( 0.000,  0.022, -0.036),
						( 0.024,  0.024, -0.024),
						( 0.039,  0.015,  0.000),
						( 0.024,  0.024,  0.024),
						(-0.015,  0.000,  0.039),
						(-0.036,  0.000,  0.022),
						(-0.036,  0.000, -0.022),
						(-0.015,  0.000, -0.039),
						( 0.000, -0.039,  0.015),
						( 0.022, -0.036,  0.000),
						( 0.000, -0.039, -0.015),
						(-0.022, -0.036,  0.000),
						( 0.000, -0.022,  0.036),
						( 0.024, -0.024,  0.024),
						( 0.039, -0.015,  0.000),
						( 0.024, -0.024, -0.024),
						( 0.000, -0.022, -0.036),
						(-0.024, -0.024, -0.024),
						(-0.039, -0.015,  0.000),
						(-0.024, -0.024,  0.024),
						( 0.015,  0.000,  0.039),
						( 0.036,  0.000,  0.022),
						( 0.036,  0.000, -0.022),
						( 0.015,  0.000, -0.039))),
    mic_pattern = 'omni'
)

miniDSP_array_setup = ArraySetup(arrayType='planar',
	orV = np.array([0.0, 0.0, 1.0]),
	mic_scale = Parameter(1),
	mic_pos = np.array((( 0.0000,  0.0430, 0.000),
						( 0.0372,  0.0215, 0.000),
						( 0.0372, -0.0215, 0.000),
						( 0.0000, -0.0430, 0.000),
						(-0.0372, -0.0215, 0.000),
						(-0.0372,  0.0215, 0.000))),
	mic_orV = np.array(((0.0, 0.0, 1.0),
						(0.0, 0.0, 1.0),
						(0.0, 0.0, 1.0),
						(0.0, 0.0, 1.0),
						(0.0, 0.0, 1.0),
						(0.0, 0.0, 1.0))),
	mic_pattern = 'omni'
)


class AcousticScene:
	""" Acoustic scene class.
	"""
	def __init__(self, room_sz, T60, beta, noise_signal, SNR, source_signal, fs, array_setup, mic_pos, timestamps, traj_pts, 
				 trajectory, t, DOA, c=343.0):
		self.room_sz = room_sz				# Room size
		self.T60 = T60						# Reverberation time of the simulated room
		self.beta = beta					# Reflection coefficients of the walls of the room (make sure it corresponds with T60)
		self.noise_signal = noise_signal	# Noise signal (nsample', 1)
		self.source_signal = source_signal  # Source signal (nsample,nsource)
		self.fs = fs						# Sampling frequency of the source signal and the simulations
		self.SNR = SNR						# Signal to (omnidirectional) noise ratio to simulate
		self.array_setup = array_setup		# Named tuple with the characteristics of the array
		self.mic_pos = mic_pos				# Position of microphones (nch,3)
		self.timestamps = timestamps		# Time of each simulation (it does not need to correspond with the DOA estimations) (npoint)
		self.traj_pts = traj_pts 			# Trajectory points to simulate (npoint,3,nsource)
		self.trajectory = trajectory		# Continuous trajectory (nsample,3,nsource)
		self.t = t							# Continuous time (nsample)
		self.DOA = DOA 						# Continuous DOA (nsample,3,nsource)
		self.c = c 							# Speed of sound 
 
	def simulate(self):
		""" Get the array recording using gpuRIR to perform the acoustic simulations.
		"""
		if self.T60 == 0:
			Tdiff = 0.1
			Tmax = 0.1
			nb_img = [1,1,1]
		else:
			Tdiff = gpuRIR.att2t_SabineEstimator(12, self.T60) # Use ISM until the RIRs decay 12dB
			Tmax = gpuRIR.att2t_SabineEstimator(40, self.T60)  # Use diffuse model until the RIRs decay 40dB
			if self.T60 < 0.15: Tdiff = Tmax # Avoid issues with too short RIRs
			nb_img = gpuRIR.t2n( Tdiff, self.room_sz )

		# nb_mics  = len(self.mic_pos)
		# nb_traj_pts = len(self.traj_pts)
		# nb_gpu_calls = min(int(np.ceil( self.fs * Tdiff * nb_mics * nb_traj_pts * np.prod(nb_img) / 1e9 )), nb_traj_pts)
		# traj_pts_batch = np.ceil( nb_traj_pts / nb_gpu_calls * np.arange(0, nb_gpu_calls+1) ).astype(int)

		num_source = self.traj_pts.shape[-1]
		RIRs_sources = []
		mic_signals_sources = []
		dp_RIRs_sources = []
		dp_mic_signals_sources = []
		nsample = len(self.t)
		for source_idx in range(num_source):
			# RIRs_list = [ 	gpuRIR.simulateRIR(self.room_sz, self.beta,
			# 				self.traj_pts[traj_pts_batch[0]:traj_pts_batch[1],:,source_idx], self.mic_pos,
			# 				nb_img, Tmax, self.fs, Tdiff=Tdiff, orV_rcv=self.array_setup.mic_orV, 
			# 				mic_pattern=self.array_setup.mic_pattern) ]
			# for i in range(1,nb_gpu_calls):
			# 	RIRs_list += [	gpuRIR.simulateRIR(self.room_sz, self.beta,
			# 					self.traj_pts[traj_pts_batch[i]:traj_pts_batch[i+1],:,source_idx], self.mic_pos,
			# 					nb_img, Tmax, self.fs, Tdiff=Tdiff, orV_rcv=self.array_setup.mic_orV, 
			# 					mic_pattern=self.array_setup.mic_pattern) ]
			# RIRs = np.concatenate(RIRs_list, axis=0)
			RIRs = gpuRIR.simulateRIR(self.room_sz, self.beta, self.traj_pts[:,:,source_idx], self.mic_pos,
						nb_img, Tmax, self.fs, Tdiff=Tdiff, orV_rcv=self.array_setup.mic_orV, 
						mic_pattern=self.array_setup.mic_pattern)
			mic_sig = gpuRIR.simulateTrajectory(self.source_signal[:,source_idx], RIRs, timestamps=self.timestamps, fs=self.fs)
			mic_sig = mic_sig[0:nsample,:]

			dp_RIRs = gpuRIR.simulateRIR(self.room_sz, self.beta, self.traj_pts[:,:,source_idx], self.mic_pos, [1,1,1],
							0.1, self.fs, orV_rcv=self.array_setup.mic_orV, mic_pattern=self.array_setup.mic_pattern)
			dp_mic_sig = gpuRIR.simulateTrajectory(self.source_signal[:,source_idx], dp_RIRs, timestamps=self.timestamps, fs=self.fs)
			dp_mic_sig = dp_mic_sig[0:nsample,:]

			RIRs_sources += [RIRs]
			mic_signals_sources += [mic_sig]
			dp_RIRs_sources += [dp_RIRs]
			dp_mic_signals_sources += [dp_mic_sig]

		RIRs_sources = np.array(RIRs_sources).transpose(1,2,3,0) # (npoints,nch,nsam,nsources)
		mic_signals_sources = np.array(mic_signals_sources).transpose(1,2,0) # (nsamples,nch,nsources)
		dp_RIRs_sources = np.array(dp_RIRs_sources).transpose(1,2,3,0)
		dp_mic_signals_sources = np.array(dp_mic_signals_sources).transpose(1,2,0)
		self.checkRIR(RIRs_sources)
		self.checkRIR(dp_RIRs_sources)

		# Add Noise
		if self.noise_signal is None:
			self.noise_signal = np.random.standard_normal(mic_sig.shape)
		mic_signals = np.sum(mic_signals_sources, axis=2) # (nsamples, nch)
		dp_mic_signals = np.sum(dp_mic_signals_sources, axis=2)
		ac_pow = np.mean([acoustic_power(dp_mic_signals[:,i]) for i in range(dp_mic_signals_sources.shape[1])])
		ac_pow_noise = np.mean([acoustic_power(self.noise_signal[:,i]) for i in range(self.noise_signal.shape[1])])
		noise_signal = np.sqrt(ac_pow/10**(self.SNR/10)) / np.sqrt(ac_pow_noise) * self.noise_signal
		mic_signals += noise_signal[0:nsample, :]

		# Apply the propagation delay to the VAD information if it exists
		if hasattr(self, 'source_vad'):
			self.mic_vad_sources = [] # for vad of separate sensor signals of source
			for source_idx in range(num_source):
				vad = gpuRIR.simulateTrajectory(self.source_vad[:,source_idx], dp_RIRs_sources[:,:,:,source_idx],
												timestamps=self.timestamps, fs=self.fs)
				vad_sources = vad[0:nsample,:].mean(axis=1) > vad[0:nsample,:].max()*1e-3
				self.mic_vad_sources += [vad_sources] # binary value
			self.mic_vad_sources = np.array(self.mic_vad_sources).transpose(1,0) # (nsample, nsources)
			self.mic_vad = np.sum(self.mic_vad_sources, axis=1)>0.5 # for vad of mixed sensor signals of sources (nsample)

		# Check whether the values of microphone signals is in the range of [-1, 1] for wav saving with soundfile.write
		# max_value = np.max(mic_signals)
		# min_value = np.min(mic_signals)
		# max_value_dp = np.max(dp_mic_signals)
		# min_value_dp = np.min(dp_mic_signals)
		# value = np.max([np.abs(max_value), np.abs(min_value), np.abs(max_value_dp), np.abs(min_value_dp)])
		# if (value>1):
		# 	print('Value of microphone signals: [{:.4f}, {:.4f}, {:.4f}, {:.4f}]'.format(min_value, max_value, min_value_dp, max_value_dp))
		# 	mic_signals = mic_signals/value
		# 	dp_mic_signals = dp_mic_signals/value
		# 	mic_signals_sources = mic_signals_sources/value
		# 	dp_mic_signals_sources = dp_mic_signals_sources/value
		# 	# raise Exception("Value of microphone signals is out of range [-1, 1]")

		return mic_signals
	
	def checkRIR(self, RIRs):
		ok_flag = True
		nan_flag = np.isnan(RIRs)
		if (True in nan_flag):
			warnings.warn('NAN exists in RIR~')
			ok_flag = False
		zero_flag = (np.sum(RIRs**2) == 0)
		if zero_flag:
			warnings.warn('RIR is all zeros~')
			ok_flag = False
		return ok_flag


# %% Source signal Datasets

class LibriSpeechDataset(Dataset):
	""" Dataset with random LibriSpeech utterances.
	"""

	def _exploreCorpus(self, path, file_extension):
		directory_tree = {}
		for item in os.listdir(path):
			if os.path.isdir( os.path.join(path, item) ):
				directory_tree[item] = self._exploreCorpus( os.path.join(path, item), file_extension )
			elif item.split(".")[-1] == file_extension:
				directory_tree[ item.split(".")[0] ] = os.path.join(path, item)
		return directory_tree

	def _cleanSilences(self, s, aggressiveness, return_vad=False):
		self.vad.set_mode(aggressiveness)

		vad_out = np.zeros_like(s)
		vad_frame_len = int(10e-3 * self.fs)  # 0.001s,16samples gives one same vad results
		n_vad_frames = len(s) // vad_frame_len # 1000/s,1/0.001s
		for frame_idx in range(n_vad_frames):
			frame = s[frame_idx * vad_frame_len: (frame_idx + 1) * vad_frame_len]
			frame_bytes = (frame * 32767).astype('int16').tobytes()
			vad_out[frame_idx*vad_frame_len: (frame_idx+1)*vad_frame_len] = self.vad.is_speech(frame_bytes, self.fs)
		s_clean = s * vad_out

		return (s_clean, vad_out) if return_vad else s_clean

	def __init__(self, path, T, fs, num_source, size=None, return_vad=False, readers_range=None, clean_silence=True):
		self.corpus = self._exploreCorpus(path, 'flac')
		if readers_range is not None:
			for key in list(map(int, self.nChapters.keys())):
				if int(key) < readers_range[0] or int(key) > readers_range[1]:
					del self.corpus[key]

		self.nReaders = len(self.corpus)
		self.nChapters = {reader: len(self.corpus[reader]) for reader in self.corpus.keys()}
		self.nUtterances = {reader: {
				chapter: len(self.corpus[reader][chapter]) for chapter in self.corpus[reader].keys()
			} for reader in self.corpus.keys()}

		self.chapterList = []
		for chapters in list(self.corpus.values()):
			self.chapterList += list(chapters.values())

		# self.readerList = []
		# for reader in self.corpus.keys():
		# 	self.readerList += list(reader)

		self.fs = fs
		self.T = T
		self.num_source = num_source

		self.clean_silence = clean_silence
		self.return_vad = return_vad
		self.vad = webrtcvad.Vad()

		self.sz = len(self.chapterList) if size is None else size

	def __len__(self):
		return self.sz

	def __getitem__(self, idx):
		if idx < 0: idx = len(self) + idx
		while idx >= len(self.chapterList): idx -= len(self.chapterList)

		s_sources = []
		s_clean_sources = []
		vad_out_sources = []
		speakerID_list = []

		for source_idx in range(self.num_source):
			if source_idx==0:
				chapter = self.chapterList[idx]
				utts = list(chapter.keys())
				spakerID = utts[0].split('-')[0]

			else:
				idx_othersources = np.random.randint(0, len(self.chapterList))
				chapter = self.chapterList[idx_othersources]
				utts = list(chapter.keys())
				spakerID = utts[0].split('-')[0]
				while spakerID in speakerID_list:
					idx_othersources = np.random.randint(0, len(self.chapterList))
					chapter = self.chapterList[idx_othersources]
					utts = list(chapter.keys())
					spakerID = utts[0].split('-')[0]

			speakerID_list += [spakerID]

			utt_paths = list(chapter.values())
			s_shape_desired = int(self.T * self.fs)
			s_clean = np.zeros((s_shape_desired,1)) # random initialization
			while np.sum(s_clean) == 0: # avoid full-zero s_clean
				# Get a random speech segment from the selected chapter
				n = np.random.randint(0, len(chapter))
				
				# s = np.array([])
				# while s.shape[0] < self.T * self.fs:
				# 	utterance, fs = soundfile.read(utt_paths[n])
				# 	assert fs == self.fs
				# 	s = np.concatenate([s, utterance])
				# 	n += 1
				# 	if n >= len(chapter): n=0
				# s = s[0: int(self.T * fs)]
				# s -= s.mean()

				s = np.array([])
				while s.shape[0] < s_shape_desired:
					utterance, fs = soundfile.read(utt_paths[n])
					assert fs == self.fs
					s = np.concatenate([s, utterance])
					n += 1
					if n >= len(chapter): n=0
				st = random.randint(0, s.shape[0] - s_shape_desired)
				ed = st + s_shape_desired
				s = s[st: ed]
				s -= s.mean()

				# Clean silences, it starts with the highest aggressiveness of webrtcvad,
				# but it reduces it if it removes more than the 66% of the samples
				s_clean, vad_out = self._cleanSilences(s, 3, return_vad=True)
				if np.count_nonzero(s_clean) < len(s_clean) * 0.66:
					s_clean, vad_out = self._cleanSilences(s, 2, return_vad=True)
				if np.count_nonzero(s_clean) < len(s_clean) * 0.66:
					s_clean, vad_out = self._cleanSilences(s, 1, return_vad=True)
	
			s_sources += [s]
			s_clean_sources += [s_clean]
			vad_out_sources += [vad_out]

		s_sources = np.array(s_sources).transpose(1,0)
		s_clean_sources = np.array(s_clean_sources).transpose(1,0)
		vad_out_sources = np.array(vad_out_sources).transpose(1,0)

		if self.clean_silence:
			return (s_clean_sources, vad_out_sources) if self.return_vad else s_clean_sources
		else:
			return (s_sources, vad_out_sources) if self.return_vad else s_sources


# %% Noise signal Datasets
class NoiseDataset():
	def __init__(self, T, fs, nmic, noise_type, noise_path=None, c=343.0):
		self.T = T
		self.fs= fs
		self.nmic = nmic
		self.noise_type = noise_type # ? 'diffuse' and 'real_world' cannot exist at the same time
		# self.mic_pos = mic_pos # valid for 'diffuse'  
		if noise_path != None:
			_, self.path_set = self._exploreCorpus(noise_path, 'wav') # valid for 'diffuse' and 'real-world'
		self.c = c

	def get_random_noise(self, mic_pos=None, eps=1e-5):
		noise_type = self.noise_type.getValue()

		if noise_type == 'spatial_white':
			noise_signal = self.gen_Gaussian_noise(self.T, self.fs, self.nmic)

		elif noise_type == 'diffuse':
			idx = random.randint(0, len(self.path_set)-1)
			noise, fs = soundfile.read(self.path_set[idx])

			nsample_desired = int(self.T * fs * self.nmic)
			noise_copy = copy.deepcopy(noise)
			nsample = noise_copy.shape[0]
			while nsample < nsample_desired:
				noise_copy = np.concatenate((noise_copy, noise), axis=0)
				nsample = noise_copy.shape[0]

			st = random.randint(0, nsample - nsample_desired)
			ed = st + nsample_desired
			noise_copy = noise_copy[st:ed]

			if fs != self.fs:
				noise_copy = scipy.signal.resample_poly(noise_copy, up=self.fs, down=fs)
				# noise_copy2 = scipy.signal.resample(noise_copy, int(len(noise_copy)/fs*self.fs))
				# noise_copy = librosa.resample(noise_copy, orig_sr = fs, target_sr = self.fs)

			noise_signal = self.gen_diffuse_noise(noise_copy, self.T, self.fs, mic_pos, c=self.c)
			noise_signal = noise_signal/(np.max(noise_signal)+eps)

		elif noise_type == 'real_world': # the array topology should be consistent
			idx = random.randint(0, len(self.path_set)-1)
			noise, fs = soundfile.read(self.path_set[idx])
			nmic = noise.shape[-1]
			if nmic != self.nmic:
				raise Exception('Unexpected number of microphone channels')
		
			nsample_desired = int(self.T * fs)
			noise_copy = copy.deepcopy(noise)
			nsample = noise.shape[0]
			while nsample < nsample_desired:
				noise_copy = np.concatenate((noise_copy, noise), axis=0)
				nsample = noise_copy.shape[0]

			st = random.randint(0, nsample - nsample_desired)
			ed = st + nsample_desired
			noise_copy = noise_copy[st:ed, :]

			if fs != self.fs:
				noise_signal = scipy.signal.resample_poly(noise_copy, up=self.fs, down=fs)
				# noise_signal = scipy.signal.resample(noise_copy, int(noise_copy.shape[0]/fs*self.fs), axis=0)
				# noise_signal = librosa.resample(noise_copy.transpose(1,0), orig_sr = fs, target_sr = self.fs).transpose(1,0)
			noise_signal = noise_signal/(np.max(noise_signal)+eps)

		else:
			raise Exception('Unknown noise type specified')

		return noise_signal

	def _exploreCorpus(self, path, file_extension):
		directory_tree = {}
		directory_path = []
		for item in os.listdir(path):
			if os.path.isdir( os.path.join(path, item) ):
				directory_tree[item], directory_path = self._exploreCorpus( os.path.join(path, item), file_extension )
			elif item.split(".")[-1] == file_extension:
				directory_tree[ item.split(".")[0] ] = os.path.join(path, item)
				directory_path += [os.path.join(path, item)]
		return directory_tree, directory_path

	def gen_Gaussian_noise(self, T, fs, nmic):

		noise = np.random.standard_normal((int(T*fs), nmic))

		return noise

	def gen_diffuse_noise(self, noise, T, fs, mic_pos, nfft=256, c=343.0, type_nf='spherical'):
		""" Reference:  E. A. P. Habets, “Arbitrary noise field generator.” https://github.com/ehabets/ANF-Generator
		"""
		M = mic_pos.shape[0]
		L = int(T*fs)
		
		# Generate M mutually 'independent' input signals
		noise = noise - np.mean(noise)
		noise_M = np.zeros([L, M])  
		for m in range(0,M):
			noise_M[:, m] = noise[m*L:(m+1)*L]

		# Generate matrix with desired spatial coherence
		w_rad = 2*math.pi*self.fs*np.array([i for i in range(nfft//2+1)])/nfft
		DC = self.gen_desired_spatial_coherence(mic_pos, type_nf, c, w_rad)

		# Generate sensor signals with desired spatial coherence
		noise_signal = self.mix_signals(noise_M, DC)

		# # Plot desired and generated spatial coherence
		# self.sc_test(DC, noise_signal, mic_idxes=[0, 1])

		return noise_signal
	
	def gen_desired_spatial_coherence(self, mic_pos, type_nf, c, w_rad):
		"""
			mic_pos: relative positions of mirophones  (nmic, 3)
			type_nf: type of noise field, 'spherical' or 'cylindrical'
			c: speed of sound
			w_rad: angular frequency in radians
		"""
		M = mic_pos.shape[0]
		# Generate matrix with desired spatial coherence
		nf = len(w_rad)
		DC = np.zeros([M, M, nf])

		# dist = np.linalg.norm(mic_pos[:, np.newaxis, :] - mic_pos[np.newaxis, :, :], axis=-1, keepdims=True)
		# if type_nf == 'spherical':
		# 	DC = np.sinc(w_rad * dist / (c * math.pi))
		# elif type_nf == 'cylindrical':
		# 	DC = scipy.special(0, w_rad * dist / c)
		# else:
		# 	raise Exception('Unknown noise field')

		for p in range(0,M):
			for q in range(0,M):
				if p == q:
					DC[p, q, :] = np.ones([1, 1, nf])
				else:
					dist = np.linalg.norm(mic_pos[p, :] - mic_pos[q, :])
					if type_nf == 'spherical':
						DC[p, q, :] = np.sinc(w_rad*dist/(c*math.pi))
					elif type_nf == 'cylindrical':
						DC[p, q, :] = scipy.special(0, w_rad*dist/c)
					else:
						raise Exception('Unknown noise field')
		return DC

	def mix_signals(self, noise, DC, method='cholesky'):
		""" Reference:  E. A. P. Habets, “Arbitrary noise field generator.” https://github.com/ehabets/ANF-Generator
			noise: noise signals (nsample, nch)
			DC: desired spatial coherence (nch, nch, nf)
			x: diffuse noise (nsample, nch)
			Note: generated spatial cohenence: cholesky is better than eigen
				sound unnaturality of sensors signals: cholesky is worse than eigen		  
		"""
		K = (DC.shape[2]-1)*2 # Number of frequency bins

		# Compute short-time Fourier transform (STFT) of all input signals
		noise = noise.transpose()
		f, t, N = scipy.signal.stft(noise, window='hann', nperseg=K, noverlap=0.75*K, nfft=K)

		# Generate output in the STFT domain for each frequency bin k
		X = np.zeros(N.shape,dtype=complex)
		for k in range(1,K//2+1):
			if method == 'cholesky':
				C = scipy.linalg.cholesky(DC[:,:,k])
			elif method == 'eigen': # Generated cohernce and noise signal are slightly different from MATLAB version
				D, V = np.linalg.eig(DC[:,:,k])
				# ind = np.argsort(D)
				# D = D[ind]
				# D = np.diag(D)
				# V = V[:, ind]
				# C = np.matmul(np.sqrt(D), V.T)
				C = V.T * np.sqrt(D)[:, np .newaxis]
			else:
				raise Exception('Unknown method specified')

			X[:,k,:] = np.dot(np.squeeze(N[:,k,:]).transpose(),np.conj(C)).transpose()

		# Compute inverse STFT
		F, df_noise = scipy.signal.istft(X,window='hann', nperseg=K, noverlap=0.75*K, nfft=K)
		df_noise = df_noise.transpose()

		return df_noise
	
	def sc_test(self, DC, df_noise, mic_idxes):
		dc = DC[mic_idxes[0], mic_idxes[1], :]
		noise = df_noise[:, mic_idxes].transpose()

		nfft = (DC.shape[2]-1)*2
		f, t, X = scipy.signal.stft(noise, window='hann', nperseg=nfft, noverlap=0.75*nfft, nfft=nfft)  # X: [M,F,T]
		phi_x = np.mean(np.abs(X)**2, axis=2)
		psi_x = np.mean(X[0, :, :] * np.conj(X[1, :, :]), axis=-1)
		sc_generated = np.real(psi_x / np.sqrt(phi_x[[0], :] * phi_x[[1], :]))

		sc_theory = dc[np.newaxis, :]

		plt.switch_backend('agg')
		plt.plot(list(range(nfft // 2 + 1)), sc_theory[0, :])
		plt.plot(list(range(nfft // 2 + 1)), sc_generated[0, :])
		plt.title(str(1))
		# plt.show()
		plt.savefig('sc')


## %% Microphone Signal Datasets
class FixMicSigDataset(Dataset):
	"""
	"""
	def __init__(self, data_dir, dataset_sz, transforms=None, return_acoustic_scene=False):
		"""
		"""
		self.transforms = transforms
		self.data_paths = []
		data_names = os.listdir(data_dir)
		for fname in data_names:
			front, ext = os.path.splitext(fname)
			if ext == '.wav':
				self.data_paths.append((os.path.join(data_dir, fname)))
		self.dataset_sz = len(self.data_paths) if dataset_sz is None else dataset_sz
		self.return_acoustic_scene = return_acoustic_scene

	def __len__(self):
		return self.dataset_sz 

	def __getitem__(self, idx):
		if idx < 0: idx = len(self) + idx

		sig_path = self.data_paths[idx]
		acous_path = sig_path.replace('wav','npz')

		acoustic_scene = AcousticScene(
					room_sz = [],
					T60 = [],
					beta = [],
					noise_signal = [],
					SNR = [],
					array_setup = [],
					mic_pos = [],
					source_signal = [],
					fs = [],
					traj_pts = [],
					timestamps = [],
					trajectory = [],
					t = [],
					DOA = [],
					c = []
				)
		mic_signals, acoustic_scene = load_file(acoustic_scene, sig_path, acous_path)

		if self.transforms is not None:
			for t in self.transforms:
				mic_signals, acoustic_scene = t(mic_signals, acoustic_scene)

		if self.return_acoustic_scene:
			return mic_signals, acoustic_scene
		else: 
			gts = {}
			gts['doa'] = acoustic_scene.DOAw.astype(np.float32)
			gts['vad_sources'] = acoustic_scene.mic_vad_sources
			# gts['vad'] = acoustic_scene.mic_vad

			return mic_signals, gts

class RandomMicSigDataset(Dataset):
	""" Dataset Acoustic Scenes with random trajectories (online generated RIR).
	"""
	def __init__(self, sourceDataset, num_source, source_state, room_sz, T60, abs_weights, array_setup, array_pos, noiseDataset, SNR, nb_points, dataset_sz, c=343.0, transforms=None, return_acoustic_scene=False, save_src_noi=False):
		"""
		sourceDataset: dataset with the source signals (such as LibriSpeechDataset)
		num_source: Number of sources
		source_state: Static or mobile sources
		room_sz: Size of the rooms in meters
		T60: Reverberation time of the room in seconds
		abs_weights: Absorption coefficients rations of the walls
		array_setup: Named tuple with the characteristics of the array
		array_pos: Position of the center of the array as a fraction of the room size
		noiseDataset: dataset with the noise signals
		SNR: Signal to (omnidirectional) noise ratio
		nb_points: Number of points to simulate along the trajectory
		c: Speed of sound 
		transforms: Transform to perform to the simulated microphone signals and the Acoustic Scene

		Any parameter (except from nb_points and transforms) can be Parameter object to make it random.
		"""
		self.sourceDataset = sourceDataset
		self.sourceDataset_idx = Parameter(range(len(self.sourceDataset)), discrete=True)
		self.source_state = source_state
		self.num_source = num_source if type(num_source) is Parameter else Parameter(num_source, discrete=True)

		self.room_sz = room_sz if type(room_sz) is Parameter else Parameter(room_sz)
		self.T60 = T60 if type(T60) is Parameter else Parameter(T60)
		self.abs_weights = abs_weights if type(abs_weights) is Parameter else Parameter(abs_weights)

		assert np.count_nonzero(array_setup.orV) == 1, "array_setup.orV mus be parallel to an axis"
		self.array_setup = array_setup
		self.N = array_setup.mic_pos.shape[0]
		self.array_pos = array_pos if type(array_pos) is Parameter else Parameter(array_pos)
		self.mic_scale = array_setup.mic_scale if type(array_setup.mic_scale) is Parameter else Parameter(array_setup.mic_scale)

		self.noiseDataset = noiseDataset
		self.SNR = SNR if type(SNR) is Parameter else Parameter(SNR)
		self.nb_points = nb_points
		self.fs = sourceDataset.fs
		self.c = c

		self.dataset_sz = dataset_sz 

		self.transforms = transforms
		self.return_acoustic_scene = return_acoustic_scene
		self.save_src_noi = save_src_noi

	def __len__(self):
		return self.dataset_sz #len(self.sourceDataset)

	def __getitem__(self, idx):
		if idx < 0: idx = len(self) + idx

		acoustic_scene = self.getRandomScene(idx)
		mic_signals = acoustic_scene.simulate()

		if self.save_src_noi == False:
			acoustic_scene.source_signal = []
			acoustic_scene.noise_signal = []
			acoustic_scene.timestamps = []
			acoustic_scene.t = []
			acoustic_scene.trajectory = []

		if self.transforms is not None:
			for t in self.transforms:
				mic_signals, acoustic_scene = t(mic_signals, acoustic_scene)
		if self.return_acoustic_scene:
			return mic_signals, acoustic_scene

		else:
			gts = {}
			gts['doa'] = acoustic_scene.DOAw.astype(np.float32)
			gts['vad_sources'] = acoustic_scene.mic_vad_sources

			return mic_signals, gts

	# def T60isValid(self, room_sz, T60, eps=1e-4):
	# 	alpha = 1
	# 	S = (room_sz[0] * room_sz[1] + room_sz[0] * room_sz[2] + room_sz[1] * room_sz[2]) * 2
	# 	V = np.prod(room_sz)
	# 	T60_min = 0.161 * V / (S * alpha) 
	# 	return bool(T60>=(T60_min-eps))  

	def T60isValid(self, room_sz, T60, alpha, th=0.005):
		Sa = (alpha[0]+alpha[1]) * room_sz[1]*room_sz[2] + \
			(alpha[2]+alpha[3]) * room_sz[0]*room_sz[2] + \
			(alpha[4]+alpha[5]) * room_sz[0]*room_sz[1]
		V = np.prod(room_sz)
		if Sa == 0: # avoid cases when all walls are reflective and T60 (from sabine eq) is extremely large
			valid_flag = False
		else:
			T60_sabine = 0.161 * V / Sa 
			# T60_min = 0.161 * V / S 
			diff = abs(T60-T60_sabine)
			valid_flag = bool(diff<th)  # false when T60<T60_min, or the abs_weights is not suited

		return valid_flag 

	def getRandomScene(self, idx):

		# Room
		T60_is_valid = False
		while(T60_is_valid==False):
			room_sz = self.room_sz.getValue()
			T60 = float(self.T60.getValue())
			abs_weights = self.abs_weights.getValue()
			beta = gpuRIR.beta_SabineEstimation(room_sz, T60, abs_weights)
			T60_is_valid = self.T60isValid(room_sz, T60, alpha=1-beta**2)

		# Microphones
		array_pos = self.array_pos.getValue() * room_sz
		mic_scale = self.mic_scale.getValue()
		mic_pos = array_pos + self.array_setup.mic_pos * mic_scale
		array_setup = ArraySetup(self.array_setup.arrayType, 
			orV = self.array_setup.orV, 
			mic_scale = mic_scale,
			mic_pos = self.array_setup.mic_pos,
			mic_orV = self.array_setup.mic_orV,
			mic_pattern = self.array_setup.mic_pattern 
		)	
		
		# Sources
		num_source = self.num_source.getValue()

		# Trajectory points
		src_pos_min = np.array([0.0, 0.0, 0.0])
		src_pos_max = room_sz
		if self.array_setup.arrayType == 'planar':
			if np.sum(self.array_setup.orV) > 0:
				src_pos_min[np.nonzero(self.array_setup.orV)] = array_pos[np.nonzero(self.array_setup.orV)]
			else:
				src_pos_max[np.nonzero(self.array_setup.orV)] = array_pos[np.nonzero(self.array_setup.orV)]
			# src_pos_min[np.nonzero(self.array_setup.orV)] += self.dis.getValue() # control the distance between sources and array

		traj_pts = np.zeros((self.nb_points, 3, num_source))
		for source_idx in range(num_source):
			if self.source_state == 'static':
				src_pos = src_pos_min + np.random.random(3) * (src_pos_max - src_pos_min)
				traj_pts[:, :, source_idx] = np.ones((self.nb_points, 1)) * src_pos

			elif self.source_state == 'mobile':
				src_pos_ini = src_pos_min + np.random.random(3) * (src_pos_max - src_pos_min)
				src_pos_end = src_pos_min + np.random.random(3) * (src_pos_max - src_pos_min)

				Amax = np.min(np.stack((src_pos_ini - src_pos_min,
											  src_pos_max - src_pos_ini,
											  src_pos_end - src_pos_min,
											  src_pos_max - src_pos_end)),
										axis=0)

				A = np.random.random(3) * np.minimum(Amax, 1) 			# Oscilations with 1m as maximum in each axis
				w = 2*np.pi / self.nb_points * np.random.random(3) * 2  # Between 0 and 2 oscilations in each axis

				traj_pts[:,:,source_idx] = np.array([np.linspace(i,j,self.nb_points) for i,j in zip(src_pos_ini, src_pos_end)]).transpose()
				traj_pts[:,:,source_idx] += A * np.sin(w * np.arange(self.nb_points)[:, np.newaxis])

				if np.random.random(1) < 0.25:
					traj_pts[:,:,source_idx] = np.ones((self.nb_points,1)) * src_pos_ini
				# traj_pts[:,2,source_idx] = array_pos[2] # if sources and array are in the same horizontal plane

		# Source signals	
		source_signal, vad = self.sourceDataset[self.sourceDataset_idx.getValue()]

		# Noise signals
		noise_signal = self.noiseDataset.get_random_noise(self.array_setup.mic_pos*mic_scale)
		SNR = self.SNR.getValue()

		# Interpolate trajectory points
		timestamps = np.arange(self.nb_points) * len(source_signal) / self.fs / self.nb_points
		t = np.arange(len(source_signal)) / self.fs
		trajectory = np.zeros((len(t), 3, num_source))
		DOA = np.zeros((len(t), 2, num_source))
		for source_idx in range(num_source):
			trajectory[:,:,source_idx]  = np.array([np.interp(t, timestamps, traj_pts[:,i,source_idx]) for i in range(3)]).transpose()
			DOA[:,:,source_idx] = cart2sph(trajectory[:,:,source_idx] - array_pos)[:, 1:3]

		acoustic_scene = AcousticScene(
			room_sz = room_sz,
			T60 = T60,
			beta = beta,
			noise_signal = noise_signal,
			SNR = SNR,
			source_signal = source_signal[:,0:num_source],
			fs = self.fs,
			array_setup = array_setup,
			mic_pos = mic_pos,
			timestamps = timestamps,
			traj_pts = traj_pts,
			t = t,
			trajectory = trajectory,
			DOA = DOA,
			c = self.c 
		)
		acoustic_scene.source_vad = vad[:,0:num_source] # a mask

		return acoustic_scene


class LocataDataset(Dataset):
	""" Dataset with the LOCATA dataset recordings and its corresponding Acoustic Scenes.
	"""
	def __init__(self, paths, array, fs, tasks=(1,3,5), recording=None, dev=False, transforms = None, return_acoustic_scene=False):
		"""
		path: path to the root of the LOCATA dataset in your file system
		array: string with the desired array ('dummy', 'eigenmike', 'benchmark2' or 'dicit')
		fs: sampling frequency (you can use it to downsample the LOCATA recordings)
		tasks: LOCATA tasks to include in the dataset (only one-source tasks are supported)
		recording: recordings that you want to include in the dataset (only supported if you selected only one task)
		dev: True if the groundtruth source positions are available
		transforms: Transform to perform to the simulated microphone signals and the Acoustic Scene
		"""
		assert array in ('dummy', 'eigenmike', 'benchmark2', 'dicit'), 'Invalid array.'
		assert recording is None or len(tasks) == 1, 'Specific recordings can only be selected for dataset with only one task'

		self.path = paths
		self.dev = dev
		self.array = array
		self.tasks = tasks
		self.transforms = transforms
		self.fs = fs

		self.vad = webrtcvad.Vad()
		self.vad.set_mode(1)

		if array == 'dummy':
			self.array_setup = dummy_array_setup
		elif array == 'eigenmike':
			self.array_setup = eigenmike_array_setup
		elif array == 'benchmark2':
			self.array_setup = benchmark2_array_setup
		elif array == 'dicit':
			self.array_setup = dicit_array_setup

		self.directories = []
		for path in paths:
			for task in tasks:
				task_path = os.path.join(path, 'task' + str(task))
				for recording in os.listdir( task_path ):
					arrays = os.listdir( os.path.join(task_path, recording) )
					if array in arrays:
						self.directories.append( os.path.join(task_path, recording, array) )
		self.directories.sort()

		self.return_acoustic_scene = return_acoustic_scene

	def __len__(self):
		return len(self.directories)

	def __getitem__(self, idx):
		directory0 = self.directories[idx]
		directory = directory0.replace('\\','/')
		mic_signals, fs = soundfile.read( os.path.join(directory, 'audio_array_' + self.array + '.wav') )

		if fs > self.fs:
			mic_signals = scipy.signal.decimate(mic_signals, int(fs/self.fs), axis=0)
			new_fs = fs / int(fs/self.fs)
			if new_fs != self.fs: warnings.warn('The actual fs is {}Hz'.format(new_fs))
			self.fs = new_fs
		elif fs < self.fs:
			raise Exception('The sampling rate of the file ({}Hz) was lower than self.fs ({}Hz'.format(fs, self.fs))

		mic_signals_ori = copy.deepcopy(mic_signals)

		# Remove initial silence
		start = np.argmax(mic_signals[:,0] > mic_signals[:,0].max()*0.15)
		mic_signals = mic_signals[start:,:]
		t = (np.arange(len(mic_signals)) + start)/self.fs

		df = pandas.read_csv( os.path.join(directory, 'position_array_' + self.array + '.txt'), sep='\t' )
		array_pos = np.stack((df['x'].values, df['y'].values,df['z'].values), axis=-1)
		array_ref_vec = np.stack((df['ref_vec_x'].values, df['ref_vec_y'].values,df['ref_vec_z'].values), axis=-1)
		array_rotation = np.zeros((array_pos.shape[0],3,3))
		for i in range(3):
			for j in range(3):
				array_rotation[:,i,j] = df['rotation_' + str(i+1) + str(j+1)]

		df = pandas.read_csv( os.path.join(directory, 'required_time.txt'), sep='\t' )
		required_time = df['hour'].values*3600+df['minute'].values*60+df['second'].values
		timestamps = required_time - required_time[0]

		if self.dev:
			sources_name = [] #loudspeaker
			for file in os.listdir( directory ):
				if file.startswith('audio_source') and file.endswith('.wav'):
					ls = file[13:-4]
					sources_name.append(ls)

			sources_signal = []
			sources_pos = []
			trajectories = []
			sensor_vads = []

			for source_name in sources_name:
				file = 'audio_source_' + source_name + '.wav'
				source_signal, fs_src = soundfile.read(os.path.join(directory, file))
				if fs_src > self.fs:
					source_signal = scipy.signal.decimate(source_signal, int(fs_src / self.fs), axis=0)
				source_signal = source_signal[start:start + len(t)]
				sources_signal.append(source_signal)

			for source_name in sources_name:
				file = 'position_source_' + source_name + '.txt'
				df = pandas.read_csv(os.path.join(directory, file), sep='\t')
				source_pos = np.stack((df['x'].values, df['y'].values, df['z'].values), axis=-1)

				sources_pos.append(source_pos)
				trajectories.append(
					np.array([np.interp(t, timestamps, source_pos[:, i]) for i in range(3)]).transpose())

			for source_name in sources_name:
				array = directory.split('/')[-1]
				file = 'VAD_' + array + '_' + source_name + '.txt'
				df = pandas.read_csv(os.path.join(directory, file), sep='\t')
				sensor_vad_ori = df['VAD'].values

				# VAD values @48kHz matched with timestamps @16kHz
				L_audio = len(sensor_vad_ori)
				t_stamps_audio = np.linspace(0,L_audio-1,L_audio) / fs_src #48 kHz
				t_stamps_opti = t + 0.0 # 16 kHz
				sensor_vad = np.zeros(len(t_stamps_opti))
				cnt = 0
				for i in range(1,L_audio):
					if t_stamps_audio[i] >= t_stamps_opti[cnt]:
						sensor_vad[cnt] = sensor_vad_ori[i - 1]
						cnt = cnt + 1
					if cnt > len(sensor_vad)-1:
						break
				if cnt <= len(sensor_vad)-1:
					VAD[cnt: ] = sensor_vad_ori[-1]
					if cnt < len(sensor_vad) - 2:
						print('Warning: VAD values do not match~')

				sensor_vads.append(sensor_vad)

			# !! the fist dimension is for sources
			sources_signal = np.stack(sources_signal)
			sources_pos = np.stack(sources_pos)
			trajectories = np.stack(trajectories)
			sensor_vads = np.stack(sensor_vads)

			DOA_pts = np.zeros(sources_pos.shape[0:2] + (2,))
			DOA = np.zeros(trajectories.shape[0:2] + (2,))
			for s in range(sources_pos.shape[0]):
				source_pos_local = np.matmul( np.expand_dims(sources_pos[s,...] - array_pos, axis=1), array_rotation ).squeeze()
				DOA_pts[s, ...] = cart2sph(source_pos_local)[:, 1:3]
				source_pos_local_interp = np.array([np.interp(t, timestamps, source_pos_local[ :, i]) for i in range(3)]).transpose()
				DOA[s, ...] = cart2sph(source_pos_local_interp)[:, 1:3]

		else:
			sources_pos = None
			DOA = None
			source_signal = np.NaN * np.ones((len(mic_signals),1))

		acoustic_scene = AcousticScene(
			room_sz = np.NaN * np.ones((3,1)),
			T60 = np.NaN,
			beta = np.NaN * np.ones((6,1)),
			noise_signal = np.NaN, 
			SNR = np.NaN,
			source_signal = sources_signal.transpose(1,0),
			fs = self.fs,
			array_setup = self.array_setup,
			mic_pos = np.NaN, # np.matmul( array_rotation[0,...], np.expand_dims(self.array_setup.mic_pos*self.array_setup.mic_scale.getValue(), axis=-1) ).squeeze() + array_pos[0,:], # Not valid for moving arrays
			timestamps = timestamps - start/self.fs, # time-stamp-wise original annonated
			traj_pts = sources_pos.transpose(1,2,0), #[0,...] # time-stamp-wise original annonated
			t = t - start/self.fs, # sample-wise remove silence
			trajectory = trajectories.transpose(1,2,0), # sample-wise remove silence
			DOA = DOA.transpose(1,2,0), #[0,...] # sample-wise remove silence
			c = np.NaN
		)

		# vad_from = 'alg'
		vad_from = 'dataset'
		if vad_from == 'alg':
			sources_signal = sources_signal.transpose(1, 0)
			vad = np.zeros_like(sources_signal)
			vad_frame_len = int(10e-3 * self.fs)
			n_vad_frames = len(source_signal) // vad_frame_len
			num_sources = sources_signal.shape[-1]
			for source_idx in range(num_sources):
				for frame_idx in range(n_vad_frames):
					frame = sources_signal[frame_idx * vad_frame_len: (frame_idx + 1) * vad_frame_len, source_idx]
					frame_bytes = (frame * 32767).astype('int16').tobytes()
					vad[frame_idx*vad_frame_len: (frame_idx+1)*vad_frame_len, source_idx] = \
						self.vad.is_speech(frame_bytes, int(self.fs))
		elif vad_from == 'dataset':
			vad = sensor_vads.transpose(1,0)

		acoustic_scene.mic_vad_sources = copy.deepcopy(vad) # (nsample, nsource)
		acoustic_scene.mic_vad = np.sum(vad, axis=1)>0.5 # for vad of mixed sensor signals of source
		
		if self.transforms is not None:
			for t in self.transforms:
				mic_signals, acoustic_scene = t(mic_signals, acoustic_scene)
		
		if self.return_acoustic_scene:
			return mic_signals.copy(), acoustic_scene
		else:
			gts = {}
			gts['doa'] = acoustic_scene.DOAw.astype(np.float32)
			gts['vad_sources'] = acoustic_scene.mic_vad_sources
						
			return mic_signals.copy(), gts # np.ascontiguousarray(mic_signals)


# %% Transform classes
class Segmenting_SRPDNN(object):
	""" Segmenting transform.
	"""
	def __init__(self, K, step, window=None):
		self.K = K
		self.step = step
		if window is None:
			self.w = np.ones(K)
		elif callable(window):
			try: self.w = window(K)
			except: raise Exception('window must be a NumPy window function or a Numpy vector with length K')
		elif len(window) == K:
			self.w = window
		else:
			raise Exception('window must be a NumPy window function or a Numpy vector with length K')

	def __call__(self, x, acoustic_scene):
		# N_mics = x.shape[1]
		N_dims = acoustic_scene.DOA.shape[1]
		num_source = acoustic_scene.DOA.shape[2]
		L = x.shape[0]
		N_w = np.floor(L/self.step - self.K/self.step + 1).astype(int)

		if self.K > L:
			raise Exception('The window size can not be larger than the signal length ({})'.format(L))
		elif self.step > L:
			raise Exception('The window step can not be larger than the signal length ({})'.format(L))

		# Pad and window the signal
		# x = np.append(x, np.zeros((N_w * self.step + self.K - L, N_mics)), axis=0)

		# shape_Xw = (N_w, self.K, N_mics)
		# strides_Xw = [self.step * N_mics, N_mics, 1]
		# strides_Xw = [strides_Xw[i] * x.itemsize for i in range(3)]
		# Xw = np.lib.stride_tricks.as_strided(x, shape=shape_Xw, strides=strides_Xw)
		# Xw = Xw.transpose((0, 2, 1)) * self.w

		# Pad and window the DOA # (nsample,naziele,nsource)
		DOA = []
		for source_idx in range(num_source):
			DOA += [np.append(acoustic_scene.DOA[:,:,source_idx], np.tile(acoustic_scene.DOA[-1,:,source_idx].reshape((1,2)),
				[N_w*self.step+self.K-L, 1]), axis=0)] # Replicate the last known DOA
		DOA = np.array(DOA).transpose(1,2,0) 

		shape_DOAw = (N_w, self.K, N_dims) # (nwindow, win_len, naziele)
		strides_DOAw = [self.step*N_dims, N_dims, 1]
		strides_DOAw = [strides_DOAw[i] * DOA.itemsize for i in range(3)]
		acoustic_scene.DOAw = [] 
		for source_idx in range(num_source):
			DOAw = np.lib.stride_tricks.as_strided(DOA[:,:,source_idx], shape=shape_DOAw, strides=strides_DOAw)
			DOAw = np.ascontiguousarray(DOAw)
			for i in np.flatnonzero(np.abs(np.diff(DOAw[..., 1], axis=1)).max(axis=1) > np.pi):
				DOAw[i, DOAw[i,:,1]<0, 1] += 2*np.pi # Avoid jumping from -pi to pi in a window
			DOAw = np.mean(DOAw, axis=1)
			DOAw[DOAw[:,1]>np.pi, 1] -= 2*np.pi
			acoustic_scene.DOAw += [DOAw]
		acoustic_scene.DOAw = np.array(acoustic_scene.DOAw).transpose(1, 2, 0) # (nsegment,naziele,nsource)

		# Pad and window the VAD if it exists
		if hasattr(acoustic_scene, 'mic_vad'): # (nsample,1)
			vad = acoustic_scene.mic_vad[:, np.newaxis] 
			vad = np.append(vad, np.zeros((L - vad.shape[0], 1)), axis=0)

			shape_vadw = (N_w, self.K, 1)
			strides_vadw = [self.step * 1, 1, 1]
			strides_vadw = [strides_vadw[i] * vad.itemsize for i in range(3)]

			acoustic_scene.mic_vad = np.lib.stride_tricks.as_strided(vad, shape=shape_vadw, strides=strides_vadw)[..., 0]

		# Pad and window the VAD if it exists
		if hasattr(acoustic_scene, 'mic_vad_sources'): # (nsample,nsource)
			shape_vadw = (N_w, self.K, 1)
			
			num_sources = acoustic_scene.mic_vad_sources.shape[1]
			vad_sources = []
			for source_idx in range(num_sources):
				vad = acoustic_scene.mic_vad_sources[:, source_idx:source_idx+1]  
				vad = np.append(vad, np.zeros((L - vad.shape[0], 1)), axis=0)

				strides_vadw = [self.step * 1, 1, 1]
				strides_vadw = [strides_vadw[i] * vad.itemsize for i in range(3)]
				vad_sources += [np.lib.stride_tricks.as_strided(vad, shape=shape_vadw, strides=strides_vadw)[..., 0]]

			acoustic_scene.mic_vad_sources = np.array(vad_sources).transpose(1,2,0) # (nsegment, nsample, nsource)

		# Timestamp for each window
		acoustic_scene.tw = np.arange(0, (L-self.K), self.step) / acoustic_scene.fs

		return x, acoustic_scene

class Segmenting(object):
	""" Segmenting transform.
	"""
	def __init__(self, K, step, window=None):
		self.K = K
		self.step = step
		if window is None:
			self.w = np.ones(K)
		elif callable(window):
			try: self.w = window(K)
			except: raise Exception('window must be a NumPy window function or a Numpy vector with length K')
		elif len(window) == K:
			self.w = window
		else:
			raise Exception('window must be a NumPy window function or a Numpy vector with length K')

	def __call__(self, x, acoustic_scene):
		N_mics = x.shape[1]
		N_dims = acoustic_scene.DOA.shape[1]
		num_source = acoustic_scene.DOA.shape[2]
		L = x.shape[0]
		N_w = np.floor(L/self.step - self.K/self.step + 1).astype(int)

		if self.K > L:
			raise Exception('The window size can not be larger than the signal length ({})'.format(L))
		elif self.step > L:
			raise Exception('The window step can not be larger than the signal length ({})'.format(L))

		# Pad and window the signal
		x = np.append(x, np.zeros((N_w * self.step + self.K - L, N_mics)), axis=0)

		shape_Xw = (N_w, self.K, N_mics)
		strides_Xw = [self.step * N_mics, N_mics, 1]
		strides_Xw = [strides_Xw[i] * x.itemsize for i in range(3)]
		Xw = np.lib.stride_tricks.as_strided(x, shape=shape_Xw, strides=strides_Xw)
		Xw = Xw.transpose((0, 2, 1)) * self.w

		# Pad and window the DOA
		DOA = []
		for source_idx in range(num_source):
			DOA += [np.append(acoustic_scene.DOA[:,:,source_idx], np.tile(acoustic_scene.DOA[-1,:,source_idx].reshape((1,2)),
				[N_w*self.step+self.K-L, 1]), axis=0)] # Replicate the last known DOA
		DOA = np.array(DOA).transpose(1,2,0)
		# L = x.shape[0]

		shape_DOAw = (N_w, self.K, N_dims)
		strides_DOAw = [self.step * N_dims, N_dims, 1]
		strides_DOAw = [strides_DOAw[i] * DOA.itemsize for i in range(3)]

		acoustic_scene.DOAw = []
		for source_idx in range(num_source):
			DOAw = np.lib.stride_tricks.as_strided(DOA[:,:,source_idx], shape=shape_DOAw, strides=strides_DOAw)
			DOAw = np.ascontiguousarray(DOAw)
			for i in np.flatnonzero(np.abs(np.diff(DOAw[..., 1], axis=1)).max(axis=1) > np.pi):
				DOAw[i, DOAw[i,:,1]<0, 1] += 2*np.pi # Avoid jumping from -pi to pi in a window
			DOAw = np.mean(DOAw, axis=1)
			DOAw[DOAw[:,1]>np.pi, 1] -= 2*np.pi
			acoustic_scene.DOAw += [DOAw]
		acoustic_scene.DOAw = np.array(acoustic_scene.DOAw).transpose(1, 2, 0)

		# Pad and window the VAD if it exists
		if hasattr(acoustic_scene, 'mic_vad'): 
			vad = acoustic_scene.mic_vad[:, np.newaxis]
			vad = np.append(vad, np.zeros((L - vad.shape[0], 1)), axis=0)

			shape_vadw = (N_w, self.K, 1)
			strides_vadw = [self.step * 1, 1, 1]
			strides_vadw = [strides_vadw[i] * vad.itemsize for i in range(3)]

			acoustic_scene.mic_vad = np.lib.stride_tricks.as_strided(vad, shape=shape_vadw, strides=strides_vadw)[..., 0]

		# Pad and window the VAD if it exists
		if hasattr(acoustic_scene, 'mic_vad_sources'): 
			shape_vadw = (N_w, self.K, 1)
			strides_vadw = [self.step * 1, 1, 1]
			strides_vadw = [strides_vadw[i] * vad.itemsize for i in range(3)]
			num_source = acoustic_scene.mic_vad_sources.shape[1]
			vad_sources = []
			for source_idx in range(num_source):
				vad = acoustic_scene.mic_vad_sources[:, source_idx:source_idx + 1]
				vad = np.append(vad, np.zeros((L - vad.shape[0], 1)), axis=0)

				vad_sources += [np.lib.stride_tricks.as_strided(vad, shape=shape_vadw, strides=strides_vadw)[..., 0]]

			acoustic_scene.mic_vad_sources = np.array(vad_sources).transpose(1, 2, 0)  # (nt,,nsource)

		# Timestamp for each window
		acoustic_scene.tw = np.arange(0, (L-self.K), self.step) / acoustic_scene.fs

		return Xw, acoustic_scene


# class Extract_DOAw(object):
# 	""" Replace the AcousticScene object by just its windowed DOA
# 	"""
# 	def __call__(self, x, acoustic_scene):
# 		return x, acoustic_scene.DOAw


# class ToFloat32(object):
# 	""" Convert to np.float32
# 	"""
# 	def __call__(self, x, DOAw):
# 		return x.astype(np.float32), DOAw.astype(np.float32)


# # %% Representation functions

# def plot_srp_map(theta, phi, srp_map, DOA=None, DOA_est=None, DOA_srpMax=None):
# 	theta = theta * 180/np.pi
# 	phi = phi * 180/np.pi
# 	theta_step = theta[1] - theta[0]
# 	phi_step = phi[1] - phi[0]
# 	plt.imshow(srp_map, cmap='inferno', extent=(phi[0]-phi_step/2, phi[-1]+phi_step/2, theta[-1]+theta_step/2, theta[0]-theta_step/2))
# 	plt.xlabel('Azimuth [º]')
# 	plt.ylabel('Elevation [º]')

# 	if DOA is not None:
# 		if DOA.ndim == 1: plt.scatter(DOA[1]*180/np.pi, DOA[0]*180/np.pi, c='r')
# 		elif DOA.ndim == 2:
# 			DOA_s = np.split(DOA, (np.abs(np.diff(DOA[:, 1])) > np.pi).nonzero()[0] + 1) # Split when jumping from -pi to pi
# 			[plt.plot(DOA_s[i][:, 1]*180/np.pi, DOA_s[i][:, 0]*180/np.pi, 'r') for i in range(len(DOA_s))]
# 			plt.scatter(DOA[-1,1]*180/np.pi, DOA[-1,0]*180 / np.pi, c='r')
# 	if DOA_srpMax is not None:
# 		if DOA_srpMax.ndim == 1: plt.scatter(DOA_srpMax[1] *180/np.pi, DOA_srpMax[0]*180/np.pi, c='k')
# 		elif DOA_srpMax.ndim == 2:
# 			DOA_srpMax_s = np.split(DOA_srpMax, (np.abs(np.diff(DOA_srpMax[:, 1])) > np.pi).nonzero()[0] + 1) # Split when jumping from -pi to pi
# 			[plt.plot(DOA_srpMax_s[i][:, 1]*180 / np.pi, DOA_srpMax_s[i][:, 0]*180 / np.pi, 'k') for i in range(len(DOA_srpMax_s))]
# 			plt.scatter(DOA_srpMax[-1,1]*180 / np.pi, DOA_srpMax[-1,0]*180 / np.pi, c='k')
# 	if DOA_est is not None:
# 		if DOA_est.ndim == 1: plt.scatter(DOA_est[1]*180/np.pi, DOA_est[0]*180/np.pi, c='b')
# 		elif DOA_est.ndim == 2:
# 			DOA_est_s = np.split(DOA_est, (np.abs(np.diff(DOA_est[:, 1])) > np.pi).nonzero()[0] + 1) # Split when jumping from -pi to pi
# 			[plt.plot(DOA_est_s[i][:, 1]*180 / np.pi, DOA_est_s[i][:, 0]*180 / np.pi, 'b') for i in range(len(DOA_est_s))]
# 			plt.scatter(DOA_est[-1,1]*180 / np.pi, DOA_est[-1,0]*180 / np.pi, c='b')

# 	plt.xlim(phi.min(), phi.max())
# 	plt.ylim(theta.max(), theta.min())
# 	plt.show()


# def animate_trajectory(theta, phi, srp_maps, fps, DOA=None, DOA_est=None, DOA_srpMax=None, file_name=None):
# 	fig = plt.figure()

# 	def animation_function(frame, theta, phi, srp_maps, DOA=None, DOA_est=None, DOA_srpMax=None):
# 		plt.clf()
# 		srp_map = srp_maps[frame,:,:]
# 		if DOA is not None: DOA = DOA[:frame+1,:]
# 		if DOA_est is not None: DOA_est = DOA_est[:frame+1,:]
# 		if DOA_srpMax is not None: DOA_srpMax = DOA_srpMax[:frame+1,:]
# 		plot_srp_map(theta, phi, srp_map, DOA, DOA_est, DOA_srpMax)

# 	anim = animation.FuncAnimation(fig, animation_function, frames=srp_maps.shape[0], fargs=(theta, phi, srp_maps, DOA, DOA_est, DOA_srpMax), interval=1e3/fps, repeat=False)
# 	plt.show()
# 	plt.close(fig)
# 	if file_name is not None: anim.save(file_name, fps=fps, extra_args=['-vcodec', 'libx264'])