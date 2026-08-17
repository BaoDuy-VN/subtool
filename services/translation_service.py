"""
Translation Service
Dịch phụ đề từ tiếng Trung sang tiếng Việt
"""
import asyncio
import re
from pathlib import Path
from typing import List, Dict, Tuple
from dataclasses import dataclass


@dataclass
class SubtitleLine:
    """Một dòng phụ đề"""
    index: int
    start_time: str
    end_time: str
    text: str
    translated_text: str = ""


class TranslationService:
    """
    Dịch phụ đề Trung → Việt
    Sử dụng deep-translator (Google Translate free)
    """
    
    def __init__(self):
        self.source_lang = "zh-CN"  # Chinese Simplified
        self.target_lang = "vi"      # Vietnamese
    
    async def translate_srt(self, srt_path: Path, output_path: Path = None) -> Path:
        """
        Dịch toàn bộ file SRT từ Trung sang Việt
        
        Args:
            srt_path: File SRT gốc (tiếng Trung)
            output_path: File SRT output (tiếng Việt), None = ghi đè
            
        Returns:
            Đường dẫn file SRT đã dịch
        """
        if output_path is None:
            output_path = srt_path.parent / f"{srt_path.stem}_vi.srt"
        
        # Parse SRT
        subtitles = self._parse_srt(srt_path)
        
        if not subtitles:
            return output_path
        
        # Dịch từng dòng (batch để nhanh hơn)
        translated = await self._translate_batch(subtitles)
        
        # Ghi file SRT mới
        self._write_srt(translated, output_path)
        
        return output_path
    
    def _parse_srt(self, srt_path: Path) -> List[SubtitleLine]:
        """Parse file SRT thành list SubtitleLine"""
        content = srt_path.read_text(encoding="utf-8")
        blocks = content.strip().split("\n\n")
        subtitles = []
        
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) >= 3:
                try:
                    index = int(lines[0])
                    times = lines[1].split(" --> ")
                    start_time = times[0].strip()
                    end_time = times[1].strip()
                    text = " ".join(lines[2:])
                    
                    subtitles.append(SubtitleLine(
                        index=index,
                        start_time=start_time,
                        end_time=end_time,
                        text=text
                    ))
                except (ValueError, IndexError):
                    continue
        
        return subtitles
    
    async def _translate_batch(self, subtitles: List[SubtitleLine], batch_size: int = 10) -> List[SubtitleLine]:
        """
        Dịch theo batch để tối ưu tốc độ
        """
        try:
            from deep_translator import GoogleTranslator
            translator = GoogleTranslator(source=self.source_lang, target=self.target_lang)
        except ImportError:
            # Fallback: không dịch, giữ nguyên
            for sub in subtitles:
                sub.translated_text = sub.text
            return subtitles
        
        # Gộp nhiều dòng thành 1 text để dịch 1 lần
        for i in range(0, len(subtitles), batch_size):
            batch = subtitles[i:i + batch_size]
            
            # Tạo text để dịch (dùng separator đặc biệt)
            separator = "\n|||\n"
            combined_text = separator.join([sub.text for sub in batch])
            
            try:
                # Dịch trong thread pool để không block
                translated_text = await asyncio.to_thread(
                    translator.translate, combined_text
                )
                
                # Tách kết quả
                translated_parts = translated_text.split("|||")
                
                # Gán kết quả cho từng subtitle
                for j, sub in enumerate(batch):
                    if j < len(translated_parts):
                        sub.translated_text = translated_parts[j].strip()
                    else:
                        sub.translated_text = sub.text  # Fallback
                        
            except Exception as e:
                print(f"Translation error at batch {i}: {e}")
                # Fallback: giữ nguyên text gốc
                for sub in batch:
                    sub.translated_text = sub.text
            
            # Delay nhỏ để tránh rate limit
            await asyncio.sleep(0.5)
        
        return subtitles
    
    def _write_srt(self, subtitles: List[SubtitleLine], output_path: Path):
        """Ghi file SRT từ list SubtitleLine"""
        with open(output_path, "w", encoding="utf-8") as f:
            for sub in subtitles:
                f.write(f"{sub.index}\n")
                f.write(f"{sub.start_time} --> {sub.end_time}\n")
                f.write(f"{sub.translated_text or sub.text}\n")
                f.write("\n")
    
    async def translate_and_burn(self, video_path: Path, srt_path: Path, 
                                output_path: Path, font_size: int = 24) -> Path:
        """
        Dịch phụ đề và burn vào video
        
        Args:
            video_path: Video gốc
            srt_path: File SRT tiếng Trung
            output_path: Video output với sub tiếng Việt
            font_size: Cỡ chữ phụ đề
            
        Returns:
            Đường dẫn video đã burn sub
        """
        # Step 1: Dịch SRT
        translated_srt = await self.translate_srt(srt_path)
        
        # Step 2: Burn sub vào video
        from services.video_editor import VideoEditor
        editor = VideoEditor()
        await editor.burn_subtitle(
            video_path=video_path,
            srt_path=translated_srt,
            output_path=output_path,
            font_size=font_size,
            position="bottom"
        )
        
        return output_path
