"""
Subtitle Extractor Service
Tách hard sub từ video bằng FFmpeg + OCR
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
    Tách hard sub từ video:
    1. Dùng FFmpeg cắt video thành frames
    2. Cắt phần dưới của frame (nơi có sub)
    3. OCR để nhận diện text
    4. So sánh giữa các frame để tìm sub mới/thay đổi
    5. Xuất file SRT
    """
    
    def __init__(self, ocr_engine: str = "easyocr"):
        """
        Args:
            ocr_engine: 'easyocr' hoặc 'tesseract'
        """
        self.ocr_engine = ocr_engine
        self.ocr_reader = None
        self.frame_dir = None
    
    async def extract(self, video_path: Path, job_id: str) -> Path:
        """
        Xử lý video và trích xuất phụ đề
        
        Args:
            video_path: Đường dẫn đến file video
            job_id: ID của job
            
        Returns:
            Đường dẫn đến file SRT đã tạo
        """
        job_dir = video_path.parent
        
        # Step 1: Lấy thông tin video
        duration = await self._get_video_duration(video_path)
        
        # Cảnh báo video dài (không chặn, để server tự xử lý)
        if duration > 300:
            print(f"Warning: Video dài {duration:.0f}s, có thể mất nhiều thời gian xử lý")
        
        # Step 2: Cắt frames (mỗi 2 giây - giảm số frame để nhanh hơn)
        self.frame_dir = job_dir / "frames"
        self.frame_dir.mkdir(exist_ok=True)
        await self._extract_frames(video_path, interval=2.0)
        
        # Step 3: OCR trên từng frame
        frame_texts = await self._ocr_frames()
        
        # Step 4: Tạo SRT từ OCR results
        entries = self._create_srt_entries(frame_texts, interval=1.0)
        
        # Step 5: Ghi file SRT
        srt_path = job_dir / f"{video_path.stem}.srt"
        self._write_srt(entries, srt_path)
        
        # Cleanup frames
        import shutil
        if self.frame_dir.exists():
            shutil.rmtree(self.frame_dir)
        
        return srt_path
    
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
    
    async def _extract_frames(self, video_path: Path, interval: float = 1.0):
        """Cắt video thành frames bằng FFmpeg"""
        output_pattern = str(self.frame_dir / "frame_%05d.jpg")
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
    
    async def _ocr_frames(self) -> List[Tuple[str, float]]:
        """
        OCR trên từng frame và trả về list (text, timestamp)
        Chỉ OCR phần dưới của frame (nơi thường có phụ đề)
        """
        frames = sorted(self.frame_dir.glob("*.jpg"))
        results = []
        
        # Import pytesseract once
        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            raise RuntimeError("pytesseract chưa được cài đặt. Chạy: pip install pytesseract Pillow")
        
        for i, frame_path in enumerate(frames):
            # Crop phần dưới (25% chiều cao frame - nơi có sub)
            cropped = self._crop_subtitle_area(frame_path)
            
            # OCR bằng Tesseract - tối ưu cho Chinese subtitles
            try:
                img = Image.open(str(cropped))
                # Chuyển sang grayscale
                img = img.convert('L')
                # Tăng độ tương phản (giúp OCR chính xác hơn)
                from PIL import ImageEnhance
                enhancer = ImageEnhance.Contrast(img)
                img = enhancer.enhance(2.0)  # Tăng contrast 2x
                # Resize xuống còn 70% (giữ đủ chi tiết cho OCR)
                img = img.resize((int(img.width * 0.7), int(img.height * 0.7)))
                # OCR với Chinese (SIM + traditional) + English
                # PSM 6: Assume a single uniform block of text
                custom_config = r'--oem 3 --psm 6'
                text = pytesseract.image_to_string(img, lang='chi_sim+chi_tra+eng', config=custom_config)
            except Exception as e:
                text = ""
            
            timestamp = i  # Mỗi frame cách nhau 2 giây
            
            # Chỉ giữ text hợp lệ (loại bỏ nhiễu)
            if text.strip() and self._is_valid_subtitle(text):
                results.append((text.strip(), timestamp))
        
        return results
    
    def _crop_subtitle_area(self, frame_path: Path) -> Path:
        """Cắt phần dưới của frame (nơi có phụ đề) - chỉ lấy 15% cuối"""
        cropped_path = frame_path.parent / f"crop_{frame_path.name}"
        
        # Dùng FFmpeg crop 15% chiều cao từ dưới lên (ít nhiễu hơn 25%)
        cmd = [
            "ffmpeg", "-i", str(frame_path),
            "-vf", "crop=iw:ih*0.15:0:ih*0.85",
            "-y", str(cropped_path)
        ]
        subprocess.run(cmd, capture_output=True, check=False)
        return cropped_path
    
    def _create_srt_entries(self, frame_texts: List[Tuple[str, float]], interval: float) -> List[SubtitleEntry]:
        """
        Tạo SRT entries từ OCR results
        Gộp các frame có cùng text thành 1 entry
        """
        if not frame_texts:
            return []
        
        entries = []
        current_text = frame_texts[0][0]
        start_time = frame_texts[0][1]
        end_time = start_time + interval
        index = 1
        
        for text, timestamp in frame_texts[1:]:
            # Nếu text giống frame trước → kéo dài end_time
            if self._is_similar(text, current_text):
                end_time = timestamp + interval
            else:
                # Text khác → lưu entry cũ, bắt đầu entry mới
                entries.append(SubtitleEntry(
                    index=index,
                    start_time=self._format_time(start_time),
                    end_time=self._format_time(end_time),
                    text=current_text
                ))
                index += 1
                current_text = text
                start_time = timestamp
                end_time = timestamp + interval
        
        # Entry cuối cùng
        entries.append(SubtitleEntry(
            index=index,
            start_time=self._format_time(start_time),
            end_time=self._format_time(end_time),
            text=current_text
        ))
        
        return entries
    
    def _is_similar(self, text1: str, text2: str, threshold: float = 0.7) -> bool:
        """Kiểm tra 2 text có giống nhau không (dùng Levenshtein ratio)"""
        if text1 == text2:
            return True
        
        # Simple similarity check
        len1, len2 = len(text1), len(text2)
        if abs(len1 - len2) > max(len1, len2) * 0.3:
            return False
        
        # Count matching characters
        matches = sum(1 for a, b in zip(text1, text2) if a == b)
        ratio = matches / max(len1, len2)
        return ratio > threshold
    
    def _is_valid_subtitle(self, text: str) -> bool:
        """Kiểm tra text có phải phụ đề hợp lệ không"""
        if not text or len(text.strip()) < 2:
            return False
        
        # Loại bỏ text chỉ chứa ký tự đặc biệt
        import re
        # Chỉ giữ text có ít nhất 2 ký tự chữ (Chinese, Vietnamese, English)
        alphanumeric = re.findall(r'[\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]', text)
        if len(alphanumeric) < 2:
            return False
        
        # Loại bỏ text quá ngắn hoặc toàn dấu gạch ngang
        if text.count('-') > len(text) * 0.5:
            return False
        
        return True
    
    def _format_time(self, seconds: float) -> str:
        """Convert giây sang format SRT time: HH:MM:SS,mmm"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    
    def _write_srt(self, entries: List[SubtitleEntry], output_path: Path):
        """Ghi file SRT"""
        with open(output_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(f"{entry.index}\n")
                f.write(f"{entry.start_time} --> {entry.end_time}\n")
                f.write(f"{entry.text}\n")
                f.write("\n")
