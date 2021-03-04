#!/usr/bin/python3
# coding=utf-8
""" PyMusicLooper
    Copyright (C) 2020  Hazem Nabil

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>."""

import os
import time
import logging

import librosa
import numpy as np
import soundfile
from pydub import AudioSegment


class MusicLooper:
    def __init__(self, filepath, min_duration_multiplier=0.35, trim=True):
        # Load the file if it exists
        if os.path.exists(filepath) and os.path.isfile(filepath):
            try:
                sound = AudioSegment.from_file(filepath)
                raw_audio, sampling_rate = sound.get_array_of_samples(), sound.frame_rate
                raw_audio = np.asarray(raw_audio, dtype=np.float32)
                raw_audio = raw_audio / max(raw_audio)
                raw_audio = raw_audio.reshape((2, raw_audio.shape[0]//2), order='F')
            except Exception as e:
                raise e
        else:
            raise FileNotFoundError("Specified file not found.")

        self.filepath = filepath
        self.filename = os.path.basename(filepath)

        mono_signal = librosa.core.to_mono(raw_audio)

        self.audio, self.trim_offset = (
            librosa.effects.trim(mono_signal, top_db=40)
            if trim
            else (mono_signal, (0, 0))
        )
        self.trim_offset = self.trim_offset[0]

        self.rate = sampling_rate
        self.min_duration_multiplier = min_duration_multiplier

        # Initialize parameters for playback
        self.playback_audio = raw_audio
        self.channels = self.playback_audio.shape[0]

    def db_diff(self, power_db_f1, power_db_f2):
        f1_max = np.max(power_db_f1)
        f2_max = np.max(power_db_f2)
        return max(f1_max, f2_max) - min(f1_max, f2_max)

    def find_loop_pairs(self):
        runtime_start = time.time()

        S = librosa.core.stft(y=self.audio)
        S_power = np.abs(S) ** 2
        S_weighed = librosa.core.perceptual_weighting(
            S=S_power, frequencies=librosa.fft_frequencies(sr=self.rate)
        )
        mel_spectrogram = librosa.feature.melspectrogram(S=S_weighed, sr=self.rate, n_mels=128, fmax=8000)
        chroma = librosa.feature.chroma_stft(S=S_power)
        power_db = librosa.power_to_db(S_weighed, ref=np.median)

        onset_env = librosa.onset.onset_strength(S=mel_spectrogram)

        pulse = librosa.beat.plp(onset_envelope=onset_env)
        beats_plp = np.flatnonzero(librosa.util.localmax(pulse))
        bpm, beats = librosa.beat.beat_track(onset_envelope=onset_env)

        beats = np.union1d(beats, beats_plp)
        beats = np.sort(beats)

        logging.info("Detected {} beats at {:.0f} bpm".format(beats.size, bpm))

        min_duration = int(chroma.shape[-1] * self.min_duration_multiplier)

        runtime_end = time.time()
        prep_time = runtime_end - runtime_start
        logging.info("Finished initial prep in {:.3}s".format(prep_time))

        candidate_pairs = []

        deviation = np.linalg.norm(chroma[..., beats] * 0.085, axis=0)

        for idx, loop_end in enumerate(beats):
            for loop_start in beats:
                if loop_end - loop_start < min_duration:
                    break
                dist = np.linalg.norm(chroma[..., loop_end] - chroma[..., loop_start])
                if dist <= deviation[idx]:
                    db_diff = self.db_diff(
                        power_db[..., loop_end], power_db[..., loop_start]
                    )
                    if db_diff <= 1.5:
                        candidate_pairs.append(
                            {
                                "loop_start": loop_start,
                                "loop_end": loop_end,
                                "dB_diff": db_diff,
                                "dist": (dist / deviation[idx])
                            }
                        )

        logging.info(f"Found {len(candidate_pairs)} possible loop points")

        if not candidate_pairs:
            return candidate_pairs

        beats_per_second = bpm / 60
        num_test_beats = 12
        seconds_to_test = num_test_beats / beats_per_second
        test_offset = librosa.samples_to_frames(int(seconds_to_test * self.rate))

        # adjust offset for very short tracks to 25% of its length
        if test_offset > chroma.shape[-1]:
            test_offset = chroma.shape[-1] // 4

        # candidate_pairs = self._dist_prune(candidate_pairs)
        candidate_pairs = self._dB_prune(candidate_pairs)

        weights = _geometric_weights(test_offset, start=test_offset // num_test_beats)
        pair_score_list = [
            self.pair_score(
                pair["loop_start"],
                pair["loop_end"],
                chroma,
                test_duration=test_offset,
                weights=weights,
            )
            for pair in candidate_pairs
        ]

        # Add cosine similarity as score
        for pair, score in zip(candidate_pairs, pair_score_list):
            pair["score"] = score

        candidate_pairs = self._score_prune(candidate_pairs)

        # re-sort based on new score
        candidate_pairs = sorted(candidate_pairs, reverse=True, key=lambda x: x["score"])

        # prefer longer loops for highly similar sequences
        if len(candidate_pairs) > 1:
            self._prioritize_duration(candidate_pairs)

        if self.trim_offset:
            for pair in candidate_pairs:
                pair["loop_start"] = self.apply_trim_offset(
                    pair["loop_start"]
                )
                pair["loop_end"] = self.apply_trim_offset(
                    pair["loop_end"]
                )

        for pair in candidate_pairs:
            logging.info(
                "Found from {} to {}, dB_diff:{}, similarity:{}".format(
                    pair["loop_start"],
                    pair["loop_end"],
                    pair["dB_diff"],
                    pair["score"],
                )
            )

        return candidate_pairs

    def _score_prune(self, candidate_pairs, percentile=10, acceptable_score=80):
        candidate_pairs = sorted(candidate_pairs, key=lambda x: x["score"])

        score_array = np.array(
            [pair["score"] for pair in candidate_pairs]
        )

        score_threshold = np.percentile(score_array, percentile, interpolation='lower')
        percentile_idx = np.searchsorted(score_array, score_threshold, side="left")
        acceptable_idx = np.searchsorted(score_array, acceptable_score, side="left")

        return candidate_pairs[min(percentile_idx, acceptable_idx):]

    def _dB_prune(self, candidate_pairs, percentile=90, acceptable_db_diff=0.125):
        candidate_pairs = sorted(candidate_pairs, key=lambda x: x["dB_diff"])

        db_diff_array = np.array(
            [pair["dB_diff"] for pair in candidate_pairs]
        )

        db_threshold = np.percentile(db_diff_array, percentile, interpolation='higher')
        percentile_idx = np.searchsorted(db_diff_array, db_threshold, side="right")
        acceptable_idx = np.searchsorted(db_diff_array, acceptable_db_diff, side="right")

        return candidate_pairs[:max(percentile_idx, acceptable_idx)]

    def _dist_prune(self, candidate_pairs, percentile=90):
        candidate_pairs = sorted(candidate_pairs, key=lambda x: x["dist"])

        dist_array = np.array(
            [pair["dist"] for pair in candidate_pairs]
        )

        dist_threshold = np.percentile(dist_array, percentile, interpolation='lower')
        percentile_idx = np.searchsorted(dist_array, dist_threshold, side="right")

        return candidate_pairs[:percentile_idx]

    def _prioritize_duration(self, pair_list):
        db_diff_array = np.array(
            [pair["dB_diff"] for pair in pair_list]
        )
        db_threshold =np.median(db_diff_array)

        duration_argmax = 0
        duration_max = 0

        score_array = np.array(
            [pair["score"] for pair in pair_list]
        )
        score_threshold = np.percentile(score_array, 90, interpolation="lower")

        score_threshold = max(score_threshold, pair_list[0]["score"] - 0.005)

        for idx, pair in enumerate(pair_list):
            if pair["score"] < score_threshold:
                break
            duration = pair["loop_end"] - pair["loop_start"]
            if duration > duration_max and pair["dB_diff"] <= db_threshold:
                duration_max, duration_argmax = duration, idx

        if duration_argmax:
            pair_list.insert(0, pair_list.pop(duration_argmax))

    def pair_score(self, b1, b2, chroma, test_duration, weights=None):
        lookahead_score = self._subseq_beat_similarity(
            b1, b2, chroma, test_duration, weights=weights
        )
        lookbehind_score = self._subseq_beat_similarity(
            b1, b2, chroma, -test_duration, weights=weights[::-1]
        )

        return max(lookahead_score, lookbehind_score)

    def _subseq_beat_similarity(self, b1_start, b2_start, chroma, test_duration, weights=None):
        if test_duration < 0:
            max_negative_offset = max(test_duration, -b1_start, -b2_start)
            b1_start = b1_start + max_negative_offset
            b2_start = b2_start + max_negative_offset

        chroma_len = chroma.shape[-1]
        test_offset = np.abs(test_duration)

        # clip to chroma len
        b1_end = min(b1_start + test_offset, chroma_len)
        b2_end = min(b2_start + test_offset, chroma_len)

        # align testing lengths
        max_offset = min(b1_end - b1_start, b2_end - b2_start)
        b1_end, b2_end = (b1_start + max_offset, b2_start + max_offset)

        dot_prod = np.einsum('ij,ij->j', 
            chroma[..., b1_start:b1_end], chroma[..., b2_start:b2_end]
        )

        b1_norm = np.linalg.norm(chroma[..., b1_start:b1_end], axis=0)
        b2_norm = np.linalg.norm(chroma[..., b2_start:b2_end], axis=0)
        cosine_sim = dot_prod / (b1_norm * b2_norm)

        if max_offset < test_offset:
            return np.average(np.pad(cosine_sim, (0, test_offset - max_offset), 'minimum'), weights=weights)
        else:
            return np.average(cosine_sim, weights=weights)

    def apply_trim_offset(self, frame):
        return (
            librosa.samples_to_frames(
                librosa.frames_to_samples(frame) + self.trim_offset
            )
            if self.trim_offset
            else frame
        )

    def samples_to_frames(self, samples):
        return librosa.core.samples_to_frames(samples)

    def frames_to_samples(self, frame):
        return librosa.core.frames_to_samples(frame)

    def seconds_to_frames(self, seconds):
        return librosa.core.time_to_frames(seconds, sr=self.rate)

    def frames_to_ftime(self, frame):
        time_sec = librosa.core.frames_to_time(frame, sr=self.rate)
        return "{:02.0f}:{:06.3f}".format(time_sec // 60, time_sec % 60)

    def play_looping(self, loop_start, loop_end, start_from=0, adjust_for_playback=False):
        import sounddevice as sd
        
        print(f"RATE: {self.rate}, CHANNELS: {self.channels}")
        blocksize = 512
        
        playback_frames = np.array_split(self.playback_audio.T, int(np.ceil(len(self.playback_audio[0, :])/blocksize))*2, axis=0)
        print(len(playback_frames))
        
        adjusted_loop_start = loop_start * self.channels
        adjusted_loop_end = loop_end * self.channels

        if adjust_for_playback:
            loop_start = loop_start * self.channels
            loop_end = loop_end * self.channels
            start_from = start_from * self.channels

        i = start_from
        loop_count = 0
        stream = sd.Stream(samplerate=self.rate, blocksize=blocksize, device=sd.default.device, channels=self.channels, dtype=np.float32)
        stream.start()
        print(adjusted_loop_end, adjusted_loop_start)
        try:
            while True:
                stream.write(playback_frames[i].copy(order='C'))
                i += 1
                if i % 1000 == 0:
                    print(i)
                if i == adjusted_loop_end:
                    i = adjusted_loop_start
                    loop_count += 1
                    print("Currently on loop #{}".format(loop_count), end="\r")
                #sd.wait()

        except KeyboardInterrupt:
            print()  # so that the program ends on a newline

    def export(
        self, loop_start, loop_end, format="WAV", output_dir=None, preserve_tags=False
    ):

        if output_dir is not None:
            out_path = os.path.join(output_dir, self.filename)
        else:
            out_path = os.path.abspath(self.filepath)

        loop_start = self.frames_to_samples(loop_start)
        loop_end = self.frames_to_samples(loop_end)

        soundfile.write(
            out_path + "-intro." + format.lower(),
            self.playback_audio[..., :loop_start].T,
            self.rate,
            format=format,
        )
        soundfile.write(
            out_path + "-loop." + format.lower(),
            self.playback_audio[..., loop_start:loop_end].T,
            self.rate,
            format=format,
        )
        soundfile.write(
            out_path + "-outro." + format.lower(),
            self.playback_audio[..., loop_end:].T,
            self.rate,
            format=format,
        )

        if preserve_tags:
            import taglib

            track = taglib.File(self.filepath)

            intro_file = taglib.File(out_path + "-intro." + format.lower())
            loop_file = taglib.File(out_path + "-loop." + format.lower())
            outro_file = taglib.File(out_path + "-outro." + format.lower())

            try:
                original_title = (
                    track.tags["TITLE"][0]
                    if track.tags is not None and len(track.tags["TITLE"]) > 0
                    else os.path.basename(self.filepath)
                )
            except KeyError:
                original_title = os.path.basename(self.filepath)

            intro_file.tags = track.tags
            loop_file.tags = track.tags
            outro_file.tags = track.tags

            intro_file.tags["TITLE"] = [original_title + " - Intro"]
            intro_file.save()

            loop_file.tags["TITLE"] = [original_title + " - Loop"]
            loop_file.save()

            outro_file.tags["TITLE"] = [original_title + " - Outro"]
            outro_file.save()

    def export_txt(self, loop_start, loop_end, output_dir=None):
        if output_dir is not None:
            out_path = os.path.join(output_dir, "loop.txt")
        else:
            out_path = os.path.join(os.path.dirname(self.filepath), "loop.txt")

        loop_start = int(self.frames_to_samples(loop_start))
        loop_end = int(self.frames_to_samples(loop_end))

        with open(out_path, "a") as file:
            file.write(f"{loop_start} {loop_end} {self.filename}\n")


def _geometric_weights(length, start=100, stop=1):
    return np.geomspace(start, stop, num=length)
