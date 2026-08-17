"""
Subtitle Extractor Service
Tách phụ đề từ video bằng Whisper AI (ASR - nhận diện giọng nói)
Giống CapCut: Audio → AI transcribe → SRT
"""
import asyncio
import subprocess
import os
import re
from pathlib import Path
from typing import List, Tuple, Optional
from dataclasses import dataclass


@dataclass
class SubtitleEntry:
    """Một dòng phụ đề với thời gian"""
    index: int
    start_time: str
    end_time: str
    text: str


class SubtitleExtractor:
    """
    Tách phụ đề từ video bằng Whisper AI:
    1. Trích xuất audio từ video bằng FFmpeg
    2. Convert sang WAV 16kHz (format Whisper yêu cầu)
    3. Dùng whisper-cli để transcribe
    4. Parse kết quả thành SRT
    """
    
    def __init__(self, model_path: str = None):
        """
        Args:
            model_path: Đường dẫn đến model Whisper (ggml-base.bin)
        """
        if model_path is None:
            # Default model path
            base_dir = Path(__file__).parent.parent
            model_path = base_dir / "models" / "ggml-base.bin"
        self.model_path = Path(model_path)
    
    async def extract(self, video_path: Path, job_id: str) -> Path:
        """
        Xử lý video và trích xuất phụ đề bằng Whisper AI
        
        Args:
            video_path: Đường dẫn đến file video
            job_id: ID của job
            
        Returns:
            Đường dẫn đến file SRT đã tạo
        """
        job_dir = video_path.parent
        
        # Step 1: Lấy thông tin video
        duration = await self._get_video_duration(video_path)
        print(f"Video duration: {duration:.1f}s")
        
        # Step 2: Trích xuất audio và convert sang WAV 16kHz
        audio_path = job_dir / "audio_16k.wav"
        await self._extract_audio(video_path, audio_path)
        
        if not audio_path.exists():
            raise RuntimeError("Không thể trích xuất audio từ video")
        
        # Step 3: Chạy Whisper CLI để transcribe
        srt_path = await self._transcribe_with_whisper(audio_path, job_dir)
        
        # Step 4: Rename SRT file
        final_srt_path = job_dir / f"{video_path.stem}.srt"
        if srt_path.exists() and srt_path != final_srt_path:
            srt_path.rename(final_srt_path)
        
        # Cleanup audio file
        if audio_path.exists():
            audio_path.unlink()
        
        return final_srt_path
    
    async def _get_video_duration(self, video_path: Path) -> float:
        """Lấy thời lượng video bằng FFprobe"""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return float(stdout.decode().strip())
    
    async def _extract_audio(self, video_path: Path, output_path: Path):
        """
        Trích xuất audio từ video và convert sang WAV 16kHz mono
        (Format Whisper yêu cầu)
        """
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vn",  # No video
            "-acodec", "pcm_s16le",  # PCM 16-bit
            "-ar", "16000",  # 16kHz sample rate
            "-ac", "1",  # Mono
            "-y", str(output_path)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
    
    async def _transcribe_with_whisper(self, audio_path: Path, output_dir: Path) -> Path:
        """
        Chạy whisper-cli để transcribe audio
        Hỗ trợ: Chinese, Vietnamese, English
        """
        # whisper-cli output format: srt
        output_prefix = str(output_dir / "whisper_output")
        
        cmd = [
            "whisper-cli",
            "-m", str(self.model_path),
            "-f", str(audio_path),
            "-osrt",  # Output SRT format
            "-of", output_prefix,  # Output file prefix
            "-l", "auto",  # Auto detect language
            "--no-prints"  # Suppress verbose output
        ]
        
        print(f"Running Whisper CLI...")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        # Check if SRT was created
        srt_path = Path(f"{output_prefix}.srt")
        if not srt_path.exists():
            print(f"Whisper stderr: {stderr.decode()}")
            raise RuntimeError("Whisper không tạo được file SRT")
        
        return srt_path
    
    def _format_time(self, seconds: float) -> str:
        """Convert giây sang format SRT time: HH:MM:SS,mmm"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
