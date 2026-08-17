"""
Dub Service
- Tách giọng nói gốc (tiếng Trung) khỏi nhạc/SFX bằng AI (Demucs)
- Tổng hợp giọng tiếng Việt từ phụ đề dịch (edge-tts)
- Ghép các câu đọc vào đúng mốc thời gian của phụ đề
"""
import asyncio
import array
import shutil
import wave
from pathlib import Path
from typing import List, Tuple, Optional, Callable


SAMPLE_RATE = 44100


class DubService:
    """Lồng tiếng Việt thay cho giọng Trung, giữ nguyên nhạc & SFX"""

    def __init__(self, voice: str = "vi-VN-HoaiMyNeural"):
        # Giọng nữ: vi-VN-HoaiMyNeural, giọng nam: vi-VN-NamMinhNeural
        self.voice = voice

    # ================= VOCAL REMOVAL =================

    async def separate_bed(self, audio_path: Path, job_dir: Path,
                           progress: Callable[[str], None] = None) -> Path:
        """
        Tách audio thành 2 phần: vocals (giọng nói) và no_vocals (nhạc+SFX).
        Trả về đường dẫn file no_vocals (phần giữ lại).
        Ưu tiên Demucs (AI); nếu không có thì fallback ffmpeg (kém hơn).
        """
        try:
            return await self._separate_demucs(audio_path, job_dir, progress)
        except Exception as e:
            print(f"Demucs failed, fallback ffmpeg: {e}")
        return await self._separate_ffmpeg(audio_path, job_dir)

    async def _separate_demucs(self, audio_path: Path, job_dir: Path,
                               progress: Callable[[str], None] = None) -> Path:
        """Dùng Demucs (Meta AI) tách vocals/no_vocals - giữ nhạc + SFX"""
        # Demucs cần Python 3.12 (venv riêng) vì Python 3.14 chưa hỗ trợ torch
        base_dir = Path(__file__).parent.parent
        demucs_python = base_dir / ".venv-demucs" / "bin" / "python"
        if not demucs_python.exists():
            raise RuntimeError("Chưa cài demucs venv")
        out_dir = job_dir / "stems"

        cmd = [
            str(demucs_python), "-m", "demucs",
            "--two-stems=vocals",
            "-o", str(out_dir),
            "-d", "cpu",
            str(audio_path)
        ]
        if progress:
            progress("Đang tách giọng nói khỏi nhạc bằng AI (lâu, 5-10 phút)...")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"demucs failed: {stderr.decode()[-500:]}")

        bed = out_dir / "htdemucs" / audio_path.stem / "no_vocals.wav"
        if not bed.exists():
            raise RuntimeError("Demucs không tạo được no_vocals.wav")

        # Chuẩn hóa về 44.1kHz stereo s16
        std = job_dir / "bed_std.wav"
        await self._to_std_wav(bed, std)
        return std

    async def _separate_ffmpeg(self, audio_path: Path, job_dir: Path) -> Path:
        """
        Fallback: khử giọng bằng center-channel cancellation (ffmpeg).
        Kém hơn Demucs (mất cả nhạc nằm giữa) nhưng không cần AI.
        """
        std = job_dir / "bed_std.wav"
        cmd = [
            "ffmpeg", "-i", str(audio_path),
            "-af", "pan=stereo|c0=c0-0.7*c1|c1=c1-0.7*c0",
            "-ar", str(SAMPLE_RATE), "-ac", "2", "-acodec", "pcm_s16le",
            "-y", str(std)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg vocal removal failed: {stderr.decode()[-300:]}")
        return std

    # ================= TTS =================

    async def synthesize_lines(self, lines: List[Tuple[float, float, str]],
                               job_dir: Path,
                               progress: Callable[[str], None] = None) -> List[Tuple[float, float, Path]]:
        """
        Đọc từng dòng phụ đề bằng edge-tts (Microsoft, miễn phí).
        Trả về list (start, end, wav_path) đã khớp độ dài ô thời gian.
        """
        import edge_tts

        tts_dir = job_dir / "tts"
        tts_dir.mkdir(exist_ok=True)
        results = []
        total = len(lines)
        sem = asyncio.Semaphore(4)  # 4 luồng song song cho nhanh
        done_count = 0

        async def process_line(i: int, start: float, end: float, text: str):
            nonlocal done_count
            text = text.strip()
            if not text:
                return None
            async with sem:
                mp3 = tts_dir / f"line_{i}.mp3"
                try:
                    communicate = edge_tts.Communicate(text, self.voice)
                    await communicate.save(str(mp3))
                except Exception as e:
                    print(f"TTS error line {i}: {e}")
                    return None

                # Convert sang wav chuẩn
                wav = tts_dir / f"line_{i}.wav"
                await self._to_std_wav(mp3, wav, mono=True)

                # Nếu câu đọc dài hơn ô thời gian -> tăng tốc (tối đa 1.8x)
                slot = end - start
                dur = self._wav_duration(wav)
                if dur > slot + 0.15 and slot > 0.3:
                    tempo = min(dur / slot, 1.8)
                    fitted = tts_dir / f"line_{i}_fit.wav"
                    await self._atempo(wav, fitted, tempo)
                    wav = fitted

                done_count += 1
                if progress and done_count % 10 == 0:
                    progress(f"Đang lồng tiếng Việt: {done_count}/{total} câu...")
                return (start, end, wav)

        tasks = [process_line(i, s, e, t) for i, (s, e, t) in enumerate(lines)]
        outputs = await asyncio.gather(*tasks)
        results = [r for r in outputs if r is not None]
        results.sort(key=lambda x: x[0])
        return results

    # ================= COMPOSE =================

    def compose_dub_track(self, clips: List[Tuple[float, float, Path]],
                          duration: float, job_dir: Path) -> Path:
        """Ghép các câu đọc thành 1 track dub stereo 44.1kHz theo đúng mốc thời gian"""
        total_samples = int(duration * SAMPLE_RATE)
        left = array.array("h", bytes(total_samples * 2))
        right = array.array("h", bytes(total_samples * 2))

        for start, end, wav_path in clips:
            rate, ch, data = self._load_wav(wav_path)
            offset = int(start * SAMPLE_RATE)
            n_frames = len(data) // ch
            for f in range(n_frames):
                idx = offset + f
                if idx >= total_samples:
                    break
                l = data[f * ch]
                r = data[f * ch + 1] if ch > 1 else l
                # Cộng dồn, chống clip
                left[idx] = max(-32767, min(32767, left[idx] + l))
                right[idx] = max(-32767, min(32767, right[idx] + r))

        # Interleave stereo
        stereo = array.array("h", bytes(total_samples * 4))
        stereo[0::2] = left
        stereo[1::2] = right

        out = job_dir / "dub_track.wav"
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(stereo.tobytes())
        return out

    # ================= HELPERS =================

    async def _to_std_wav(self, src: Path, dst: Path, mono: bool = False):
        cmd = [
            "ffmpeg", "-i", str(src),
            "-ar", str(SAMPLE_RATE),
            "-ac", "1" if mono else "2",
            "-acodec", "pcm_s16le",
            "-y", str(dst)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    async def _atempo(self, src: Path, dst: Path, tempo: float):
        cmd = [
            "ffmpeg", "-i", str(src),
            "-filter:a", f"atempo={tempo:.2f}",
            "-y", str(dst)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

    def _load_wav(self, path: Path) -> Tuple[int, int, array.array]:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            ch = wf.getnchannels()
            data = array.array("h")
            data.frombytes(wf.readframes(wf.getnframes()))
        return rate, ch, data

    def _wav_duration(self, path: Path) -> float:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / wf.getframerate()

    @staticmethod
    def parse_srt_times(srt_path: Path) -> List[Tuple[float, float, str]]:
        """Parse SRT -> list (start_s, end_s, text)"""
        def to_sec(t: str) -> float:
            h, m, rest = t.strip().split(":")
            s, ms = rest.split(",")
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

        content = srt_path.read_text(encoding="utf-8")
        blocks = content.strip().split("\n\n")
        lines = []
        for block in blocks:
            parts = block.strip().split("\n")
            if len(parts) < 3:
                continue
            try:
                times = parts[1].split(" --> ")
                lines.append((to_sec(times[0]), to_sec(times[1]), " ".join(parts[2:])))
            except (ValueError, IndexError):
                continue
        return lines
