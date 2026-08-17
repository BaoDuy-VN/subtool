"""
Video Editor Service
Cung cấp các tính năng chỉnh sửa video bằng FFmpeg
"""
import asyncio
import subprocess
import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


class VideoEditor:
    """
    Video Editor với các tính năng:
    1. Gắn phụ đề vào video (burn subtitle)
    2. Cắt video (trim)
    3. Thêm watermark/text
    4. Ghép nhiều video (concat)
    5. Tách audio
    6. Điều chỉnh tốc độ
    7. Chuyển đổi format
    8. Nén video
    9. Resize/crop
    10. Thêm nhạc nền
    """
    
    def __init__(self):
        self.output_dir = Path(__file__).parent.parent / "static" / "uploads"
    
    async def get_video_info(self, video_path: Path) -> Dict:
        """Lấy thông tin chi tiết của video"""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(video_path)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode())
        
        # Tìm video stream
        video_stream = None
        audio_stream = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video" and not video_stream:
                video_stream = stream
            elif stream.get("codec_type") == "audio" and not audio_stream:
                audio_stream = stream
        
        return {
            "duration": float(data.get("format", {}).get("duration", 0)),
            "size": int(data.get("format", {}).get("size", 0)),
            "format": data.get("format", {}).get("format_name", ""),
            "width": video_stream.get("width", 0) if video_stream else 0,
            "height": video_stream.get("height", 0) if video_stream else 0,
            "fps": self._parse_fps(video_stream.get("r_frame_rate", "0/1")) if video_stream else 0,
            "video_codec": video_stream.get("codec_name", "") if video_stream else "",
            "audio_codec": audio_stream.get("codec_name", "") if audio_stream else "",
            "has_audio": audio_stream is not None
        }
    
    def _parse_fps(self, fps_str: str) -> float:
        """Parse FPS từ format 'num/den'"""
        try:
            num, den = fps_str.split("/")
            return float(num) / float(den) if float(den) != 0 else 0
        except:
            return 0
    
    async def burn_subtitle(self, video_path: Path, srt_path: Path, output_path: Path, 
                           font_size: int = 48, font_color: str = "white",
                           position: str = "bottom", outline: bool = True,
                           remove_hardsub: bool = False) -> Path:
        """
        Gắn phụ đề vào video (burn subtitle)
        
        Args:
            video_path: Video gốc
            srt_path: File SRT phụ đề
            output_path: Đường dẫn output
            font_size: Cỡ chữ tính theo pixel ở độ phân giải gốc
            font_color: Màu chữ (white, yellow, etc.)
            position: Vị trí (top, middle, bottom)
            outline: Có viền chữ không
            remove_hardsub: Làm mờ vùng sub cứng gốc ở đáy video
        """
        # Scale font_size (pixel) sang đơn vị ASS (PlayResY=288) theo chiều cao video
        info = await self.get_video_info(video_path)
        height = info.get("height") or 1080
        ass_font = max(int(font_size * 288 / height), 6)
        
        # Force style cho subtitle
        force_style = f"FontSize={ass_font},PrimaryColour=&H00FFFFFF"
        if outline:
            force_style += ",Outline=2,OutlineColour=&H00000000"
        
        # Vị trí subtitle
        if position == "top":
            force_style += ",Alignment=8"  # Top center
        elif position == "middle":
            force_style += ",Alignment=5"  # Middle center
        else:
            # Bottom center - MarginV ~3.5% chiều cao để sub nằm gọn trong vùng mờ
            force_style += ",Alignment=2,MarginV=10"
        
        # Escape đường dẫn cho filter subtitles (tránh lỗi parse của ffmpeg)
        srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        
        if remove_hardsub:
            # Làm mờ dải 13% ở đáy (0.85→0.98) - vừa khung sub cứng gốc,
            # rồi gắn sub mới đè lên đúng chỗ đó
            filter_complex = (
                "[0:v]split=2[base][subsrc];"
                "[subsrc]crop=iw:ih*0.13:0:ih*0.85,boxblur=20:2[blurred];"
                "[base][blurred]overlay=0:H*0.85[bg];"
                f"[bg]subtitles='{srt_escaped}':force_style='{force_style}'[out]"
            )
            cmd = [
                "ffmpeg", "-i", str(video_path),
                "-filter_complex", filter_complex,
                "-map", "[out]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "copy",
                "-y", str(output_path)
            ]
        else:
            cmd = [
                "ffmpeg", "-i", str(video_path),
                "-vf", f"subtitles='{srt_escaped}':force_style='{force_style}'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",  # Encode nhanh, chất lượng tốt
                "-c:a", "copy",
                "-y", str(output_path)
            ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg burn subtitle failed: {stderr.decode()[-800:]}")
        return output_path
    
    async def mix_audio(self, video_path: Path, bed_path: Path, dub_path: Path,
                        output_path: Path) -> Path:
        """
        Thay âm thanh video bằng: nhạc/SFX gốc (bed) + giọng lồng tiếng Việt (dub).
        Video stream giữ nguyên (copy) nên nhanh.
        """
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-i", str(bed_path),
            "-i", str(dub_path),
            "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:normalize=0,volume=1.5[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-y", str(output_path)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg mix audio failed: {stderr.decode()[-500:]}")
        return output_path
    
    async def trim_video(self, video_path: Path, output_path: Path,
                        start_time: float, end_time: float = None) -> Path:
        """
        Cắt video theo thời gian
        
        Args:
            video_path: Video gốc
            output_path: Đường dẫn output
            start_time: Thời gian bắt đầu (giây)
            end_time: Thời gian kết thúc (giây), None = đến hết
        """
        cmd = ["ffmpeg", "-i", str(video_path)]
        
        if start_time > 0:
            cmd.extend(["-ss", str(start_time)])
        
        if end_time:
            duration = end_time - start_time
            cmd.extend(["-t", str(duration)])
        
        cmd.extend(["-c", "copy", "-y", str(output_path)])
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def add_watermark(self, video_path: Path, output_path: Path,
                           text: str = None, position: str = "bottom_right",
                           font_size: int = 24, opacity: float = 0.8) -> Path:
        """
        Thêm watermark text vào video
        
        Args:
            video_path: Video gốc
            output_path: Đường dẫn output
            text: Text watermark
            position: Vị trí (top_left, top_right, bottom_left, bottom_right, center)
            font_size: Cỡ chữ
            opacity: Độ trong suốt (0-1)
        """
        # Vị trí text
        positions = {
            "top_left": "x=20:y=20",
            "top_right": "x=w-tw-20:y=20",
            "bottom_left": "x=20:y=h-th-20",
            "bottom_right": "x=w-tw-20:y=h-th-20",
            "center": "x=(w-tw)/2:y=(h-th)/2"
        }
        pos = positions.get(position, positions["bottom_right"])
        
        # Tạo drawtext filter
        drawtext = f"drawtext=text='{text}':{pos}:fontsize={font_size}:fontcolor=white@{opacity}:borderw=2:bordercolor=black@0.5"
        
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vf", drawtext,
            "-c:a", "copy",
            "-y", str(output_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def concat_videos(self, video_paths: List[Path], output_path: Path) -> Path:
        """
        Ghép nhiều video lại với nhau
        
        Args:
            video_paths: List các video cần ghép
            output_path: Đường dẫn output
        """
        # Tạo file list cho FFmpeg concat
        list_file = output_path.parent / "concat_list.txt"
        with open(list_file, "w") as f:
            for vp in video_paths:
                f.write(f"file '{vp}'\n")
        
        cmd = [
            "ffmpeg", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            "-y", str(output_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        
        # Cleanup
        if list_file.exists():
            list_file.unlink()
        
        return output_path
    
    async def extract_audio(self, video_path: Path, output_path: Path, 
                           format: str = "mp3", bitrate: str = "192k") -> Path:
        """
        Tách audio từ video
        
        Args:
            video_path: Video gốc
            output_path: Đường dẫn output
            format: Định dạng audio (mp3, wav, aac, m4a)
            bitrate: Bitrate audio
        """
        audio_codecs = {
            "mp3": "libmp3lame",
            "wav": "pcm_s16le",
            "aac": "aac",
            "m4a": "aac",
            "ogg": "libvorbis"
        }
        
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vn",  # No video
            "-acodec", audio_codecs.get(format, "libmp3lame"),
            "-ab", bitrate,
            "-y", str(output_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def change_speed(self, video_path: Path, output_path: Path,
                          speed: float = 1.0) -> Path:
        """
        Điều chỉnh tốc độ video
        
        Args:
            video_path: Video gốc
            output_path: Đường dẫn output
            speed: Tốc độ (0.5 = chậm 2x, 2.0 = nhanh 2x)
        """
        # Video filter: setpts=PTS/speed
        # Audio filter: atempo=speed (chỉ hỗ trợ 0.5-2.0)
        video_filter = f"setpts=PTS/{speed}"
        
        # atempo chỉ hỗ trợ 0.5-2.0, cần chain nhiều filter cho speed ngoài khoảng
        audio_filters = []
        remaining_speed = speed
        while remaining_speed > 2.0:
            audio_filters.append("atempo=2.0")
            remaining_speed /= 2.0
        while remaining_speed < 0.5:
            audio_filters.append("atempo=0.5")
            remaining_speed *= 2.0
        audio_filters.append(f"atempo={remaining_speed}")
        audio_filter = ",".join(audio_filters)
        
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-filter_complex", f"[0:v]{video_filter}[v];[0:a]{audio_filter}[a]",
            "-map", "[v]", "-map", "[a]",
            "-y", str(output_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def convert_format(self, video_path: Path, output_path: Path,
                            codec: str = "libx264", quality: str = "23") -> Path:
        """
        Chuyển đổi format video
        
        Args:
            video_path: Video gốc
            output_path: Đường dẫn output
            codec: Video codec (libx264, libx265, libvpx-vp9)
            quality: Chất lượng (CRF 0-51, thấp hơn = tốt hơn)
        """
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-c:v", codec,
            "-crf", quality,
            "-preset", "fast",
            "-c:a", "aac",
            "-y", str(output_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def compress_video(self, video_path: Path, output_path: Path,
                            target_size_mb: float = None, crf: int = 28) -> Path:
        """
        Nén video giảm dung lượng
        
        Args:
            video_path: Video gốc
            output_path: Đường dẫn output
            target_size_mb: Dung lượng mục tiêu (MB), None = dùng CRF
            crf: Constant Rate Factor (0-51, cao hơn = nén nhiều hơn)
        """
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-c:v", "libx264",
            "-crf", str(crf),
            "-preset", "medium",
            "-c:a", "aac", "-b:a", "128k",
            "-y", str(output_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def resize_video(self, video_path: Path, output_path: Path,
                          width: int = None, height: int = None,
                          keep_aspect: bool = True) -> Path:
        """
        Resize video
        
        Args:
            video_path: Video gốc
            output_path: Đường dẫn output
            width: Chiều rộng mới
            height: Chiều cao mới
            keep_aspect: Giữ tỷ lệ khung hình
        """
        if keep_aspect:
            if width:
                scale_filter = f"scale={width}:-2"
            elif height:
                scale_filter = f"scale=-2:{height}"
            else:
                scale_filter = "scale=1280:-2"
        else:
            scale_filter = f"scale={width or 1280}:{height or 720}"
        
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vf", scale_filter,
            "-c:a", "copy",
            "-y", str(output_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def add_background_music(self, video_path: Path, audio_path: Path,
                                  output_path: Path, volume: float = 0.5,
                                  loop: bool = True) -> Path:
        """
        Thêm nhạc nền vào video
        
        Args:
            video_path: Video gốc
            audio_path: File nhạc nền
            output_path: Đường dẫn output
            volume: Âm lượng nhạc nền (0-1)
            loop: Lặp lại nhạc nếu video dài hơn nhạc
        """
        # Stream input: -stream_loop -1 để loop audio
        loop_flag = "-stream_loop -1" if loop else ""
        
        cmd = f"""
        ffmpeg -i "{video_path}" {loop_flag} -i "{audio_path}" \
            -filter_complex "[1:a]volume={volume}[music];[0:a][music]amix=inputs=2:duration=first[aout]" \
            -map 0:v -map "[aout]" -c:v copy -y "{output_path}"
        """
        
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return output_path
    
    async def extract_frames(self, video_path: Path, output_dir: Path,
                            interval: float = 1.0, format: str = "jpg") -> List[Path]:
        """
        Trích xuất frames từ video
        
        Args:
            video_path: Video gốc
            output_dir: Thư mục lưu frames
            interval: Khoảng cách giữa các frame (giây)
            format: Định dạng ảnh (jpg, png)
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = str(output_dir / f"frame_%05d.{format}")
        
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vf", f"fps=1/{interval}",
            "-q:v", "2",
            output_pattern
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        
        return sorted(output_dir.glob(f"*.{format}"))
