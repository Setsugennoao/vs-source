import json
import atexit
import subprocess
from pathlib import Path
import vapoursynth as vs
from io import BufferedReader
from pyparsedvd import vts_ifo
from os import name as os_name
from lvsfunc.types import Range
from itertools import accumulate
from abc import abstractmethod
from typing import List, Union, Optional, Tuple, cast

from .DVDIndexers import DVDIndexer, D2VWitch

core = vs.core


class __IsoFile:
    def __init__(self, path: Path, indexer: DVDIndexer = D2VWitch(), safe_index: bool = False):
        self.iso_path = Path(path)
        if not self.iso_path.is_dir() and not self.iso_path.is_file():
            raise ValueError(
                "IsoFile: path needs to point to a .ISO or a dir root of DVD"
            )

        self.indexer = indexer
        self.safe_index = safe_index
        self.__idx_path = None       # type: Optional[Path]
        self.__mount_path = None     # type: Optional[Path]
        self.__clip = None           # type: Optional[vs.VideoNode]
        self.split_clips = None      # type: Optional[List[vs.VideoNode]]
        self.joined_clip = None      # type: Optional[vs.VideoNode]
        self.split_chapters = None   # type: Optional[List[List[int]]]
        self.joined_chapters = None  # type: Optional[List[int]]

    def source(self):
        if self.__mount_path is None:
            self.__mount_path = self._get_mount_path()

        vob_files = [
            f for f in sorted(self.__mount_path.glob('*.vob')) if f.stem != 'VIDEO_TS'
        ]

        if not len(vob_files):
            raise FileNotFoundError('IsoFile: No VOBs found!')

        self.__idx_path = self.indexer.get_idx_file_path(self.iso_path)

        if not self.__idx_path.is_file():
            self.indexer.index(vob_files, self.__idx_path)
        else:
            self.indexer.update_idx_file(self.__idx_path, vob_files)

        self.__clip = self.indexer.vps_indexer(self.__idx_path)

        return self.__clip

    def __split_chapters_clips(self, split_chapters: List[List[int]],
                               dvd_menu_length: int) -> Tuple[List[List[int]], List[vs.VideoNode]]:
        self.__clip = cast(vs.VideoNode, self.__clip)
        self.__idx_path = cast(Path, self.__idx_path)

        durations = list(accumulate([0] + [frame[-1] for frame in split_chapters]))

        # Remove splash screen and DVD Menu
        clip = self.__clip[dvd_menu_length:]

        # Trim per title
        clips = [clip[s:e] for s, e in zip(durations[:-1], durations[1:])]

        if dvd_menu_length:
            clips.append(self.__clip[:dvd_menu_length])
            split_chapters.append([0, dvd_menu_length])

        return split_chapters, clips

    def split_titles(self) -> Tuple[List[vs.VideoNode], List[List[int]], vs.VideoNode, List[int]]:
        if self.__idx_path is None:
            self.__idx_path = self.indexer.get_idx_file_path(self.iso_path)

        if self.__mount_path is None:
            self.__mount_path = self._get_mount_path()

        if self.__clip is None:
            self.__clip = self.source()

        ifo_files = [
            f for f in sorted(self.__mount_path.glob('*.ifo')) if f.stem != 'VIDEO_TS'
        ]

        program_chains = []

        m_ifos = len(ifo_files) > 1

        for ifo_file in ifo_files:
            with open(ifo_file, 'rb') as file:
                curr_pgci = vts_ifo.load_vts_pgci(cast(BufferedReader, file))
                program_chains += curr_pgci.program_chains[int(m_ifos):]

        split_chapters: List[List[int]] = []

        for prog in program_chains:
            dvd_fps_s = [pb_time.fps for pb_time in prog.playback_times]
            if all(dvd_fps_s[0] == dvd_fps for dvd_fps in dvd_fps_s):
                fps = vts_ifo.FRAMERATE[dvd_fps_s[0]]
            else:
                raise ValueError('IsoFile: No VFR allowed!')

            raw_fps = 30 if fps.numerator == 30000 else 25

            split_chapters.append([0] + [
                pb_time.frames + (pb_time.hours * 3600 + pb_time.minutes * 60 + pb_time.seconds) * raw_fps
                for pb_time in prog.playback_times
            ])

        split_chapters = [
            list(accumulate(chapter_frames)) for chapter_frames in split_chapters
        ]

        idx_info = self.indexer.get_info(self.__idx_path, 0)

        vts_0_size = idx_info.videos[0].size

        dvd_menu_length = len(idx_info.data) if vts_0_size > 2 << 12 else 0

        self.split_chapters, self.split_clips = self.__split_chapters_clips(split_chapters, dvd_menu_length)

        def __gen_joined_clip():
            split_clips = cast(List[vs.VideoNode], self.split_clips)
            joined_clip = split_clips[0]

            if len(split_clips) > 1:
                for cclip in split_clips[1:]:
                    joined_clip += cclip

            return joined_clip

        def __gen_joined_chapts():
            spl_chapts = cast(List[List[int]], self.split_chapters)
            joined_chapters = spl_chapts[0]

            if len(spl_chapts) > 1:
                for rrange in spl_chapts[1:]:
                    joined_chapters += [
                        r + joined_chapters[-1] for r in rrange if r != 0
                    ]

            return joined_chapters

        self.joined_clip = __gen_joined_clip()
        self.joined_chapters = __gen_joined_chapts()

        if self.joined_chapters[-1] > self.__clip.num_frames:
            if not self.safe_index:
                print(Warning(
                    "\n\tIsoFile: The chapters are broken, last few chapters "
                    "and negative indices will probably give out an error. "
                    "You can set safe_indices = True and trim down the chapters.\n"
                ))
            else:
                offset = 0
                split_chapters = [[] for _ in range(len(self.split_chapters))]

                for i in range(len(self.split_chapters)):
                    for j in range(len(self.split_chapters[i])):
                        if self.split_chapters[i][j] + offset < self.__clip.num_frames:
                            split_chapters[i].append(self.split_chapters[i][j])
                        else:
                            print(self.__clip.num_frames, dvd_menu_length, len(self.split_chapters), i)
                            split_chapters[i].append(
                                self.__clip.num_frames - dvd_menu_length - len(self.split_chapters) + i + 2
                            )

                            print(i + 1, len(self.split_chapters) - 1)

                            for k in range(i + 1, len(self.split_chapters) - (int(dvd_menu_length > 0))):
                                split_chapters[k] = [0, 1]

                            if dvd_menu_length:
                                split_chapters[-1] = self.split_chapters[-1]

                            break
                    else:
                        offset += self.split_chapters[i][-1]
                        continue
                    break

                self.split_chapters, self.split_clips = self.__split_chapters_clips(
                    split_chapters if dvd_menu_length == 0 else split_chapters[:-1],
                    dvd_menu_length
                )

                print(len(self.split_clips))

                print(self.split_chapters)

                self.joined_clip = __gen_joined_clip()
                self.joined_chapters = __gen_joined_chapts()

                print(self.joined_chapters)

        return self.split_clips, self.split_chapters, self.joined_clip, self.joined_chapters

    def get_title(
        self, clip_index: Optional[int] = None, chapters: Optional[Union[Range, List[Range]]] = None
    ) -> Union[vs.VideoNode, List[vs.VideoNode]]:
        if not self.__clip:
            self.__clip = self.source()

        if not self.split_clips:
            self.split_titles()

        if clip_index is not None:
            ranges = cast(List[List[int]], self.split_chapters)[clip_index]
            clip = cast(List[vs.VideoNode], self.split_clips)[clip_index]
        else:
            ranges = cast(List[int], self.joined_chapters)
            clip = cast(vs.VideoNode, self.joined_clip)

        rlength = len(ranges)

        if isinstance(chapters, int):
            start, end = ranges[0], ranges[-1]

            if chapters == rlength - 1:
                start = ranges[-2]
            elif chapters == 0:
                end = ranges[1]
            elif chapters < 0:
                start = ranges[rlength - 1 + chapters]
                end = ranges[rlength + chapters]
            else:
                start = ranges[chapters]
                end = ranges[chapters + 1]

            return clip[start:end]
        elif isinstance(chapters, tuple):
            start, end = chapters

            if start is None:
                start = 0
            elif start < 0:
                start = rlength - 1 + start

            if end is None:
                end = rlength - 1
            elif end < 0:
                end = rlength - 1 + end
            else:
                end += 1

            return clip[ranges[start]:ranges[end]]
        elif isinstance(chapters, list):
            return [cast(vs.VideoNode, self.get_title(clip_index, rchap)) for rchap in chapters]

        return clip

    @abstractmethod
    def _get_mount_path(self) -> Path:
        raise NotImplementedError()


class __WinIsoFile(__IsoFile):
    class_mount: bool = False

    def _get_mount_path(self) -> Path:
        if self.iso_path.is_dir():
            return self.iso_path

        disc = self.__get_mounted_disc()

        if not disc:
            self.class_mount = True
            disc = self.__mount()

        if not disc:
            raise RuntimeError("IsoFile: Could not mount ISO file!")

        if self.class_mount:
            atexit.register(self.__unmount)

        return Path(fr"{disc['DriveLetter']}:\VIDEO_TS")

    def __run_disc_util(self, iso_path: Path, util: str) -> Optional[dict]:
        process = subprocess.Popen([
            "PowerShell", fr'{util}-DiskImage -ImagePath "{str(iso_path)}" | Get-Volume | ConvertTo-Json'],
            stdout=subprocess.PIPE
        )

        bjson, err = process.communicate()

        if err or bjson == b'' or str(bjson[:len(util)], 'utf8') == util:
            return None

        bjson = json.loads(str(bjson, 'utf-8'))

        del bjson['CimClass'], bjson['CimInstanceProperties'], bjson['CimSystemProperties']

        return bjson

    def __get_mounted_disc(self):
        return self.__run_disc_util(self.iso_path, 'Get')

    def __mount(self):
        return self.__run_disc_util(self.iso_path, 'Mount')

    def __unmount(self):
        return self.__run_disc_util(self.iso_path, 'Dismount')


class __LinuxIsoFile(__IsoFile):
    def __init__(self, path):
        raise NotImplementedError(
            "IsoFile: Linux filesystem not (yet) supported."
        )


IsoFile = __WinIsoFile if os_name == 'nt' else __LinuxIsoFile