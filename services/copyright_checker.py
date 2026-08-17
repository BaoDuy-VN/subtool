"""
Copyright Checker Service
Kiểm tra bản quyền video/nhạc bằng audio fingerprinting
"""
import asyncio
import subprocess
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict


@dataclass
class CopyrightResult:
    """Kết quả kiểm tra bản quyền"""
    is_copyrighted: bool
    confidence: float  # 0.0 - 1.0
    matches: List[Dict]
    warnings: List[str]
    details: Dict


class CopyrightChecker:
    """
    Kiểm tra bản quyền video/nhạc:
    1. Trích xuất audio từ video
    2. Tạo audio fingerprint (hash)
    3. So sánh với database nhạc có bản quyền
    4. Kiểm tra qua YouTube Data API (nếu có key)
    """
    
    def __init__(self):
        self.youtube_api_key = os.getenv("YOUTUBE_API_KEY", "")
        self.fingerprint_db_path = Path(__file__).parent.parent / "data" / "fingerprint_db.json"
    
    async def check(self, file_path: Path, job_id: str) -> Dict:
        """
        Kiểm tra bản quyền cho file
        
        Args:
            file_path: Đường dẫn file video/audio
            job_id: ID của job
            
        Returns:
            Dict chứa kết quả kiểm tra
        """
        # Step 1: Trích xuất audio
        audio_path = await self._extract_audio(file_path)
        
        if not audio_path.exists():
            return {
                "is_copyrighted": False,
                "confidence": 0,
                "matches": [],
                "warnings": ["Không thể trích xuất audio từ file. Video có thể quá ngắn hoặc không có audio."],
                "details": {"error": "audio_extraction_failed"}
            }
        
        # Step 2: Tạo audio fingerprint
        fingerprint = await self._create_fingerprint(audio_path)
        
        # Step 3: Phân tích audio
        audio_info = await self._analyze_audio(audio_path)
        
        # Step 4: Kiểm tra với database nội bộ
        local_matches = await self._check_local_db(fingerprint)
        
        # Step 5: Kiểm tra với YouTube API (nếu có key)
        youtube_matches = []
        if self.youtube_api_key:
            youtube_matches = await self._check_youtube(audio_path, audio_info)
        
        # Step 6: Tổng hợp kết quả
        result = self._compile_result(local_matches, youtube_matches, audio_info)
        
        # Cleanup audio file
        if audio_path.exists() and audio_path != file_path:
            audio_path.unlink()
        
        return result
    
    async def _extract_audio(self, file_path: Path) -> Path:
        """Trích xuất audio từ video"""
        audio_path = file_path.parent / f"{file_path.stem}_audio.mp3"
        
        cmd = [
            "ffmpeg", "-i", str(file_path),
            "-vn",  # No video
            "-acodec", "libmp3lame",
            "-ab", "128k",
            "-y",
            str(audio_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        
        return audio_path
    
    async def _create_fingerprint(self, audio_path: Path) -> Dict:
        """
        Tạo audio fingerprint
        Sử dụng chromaprint/acoustid hoặc đơn giản là hash các đoạn audio
        """
        # Tạo hash của toàn bộ file
        file_hash = hashlib.sha256()
        with open(audio_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                file_hash.update(chunk)
        
        # Tạo hash của các đoạn 30 giây (để so sánh từng phần)
        duration = await self._get_audio_duration(audio_path)
        segment_hashes = []
        
        for start in range(0, int(duration), 30):
            segment_hash = await self._hash_audio_segment(audio_path, start, 30)
            segment_hashes.append({
                "start": start,
                "duration": min(30, duration - start),
                "hash": segment_hash
            })
        
        return {
            "file_hash": file_hash.hexdigest(),
            "segment_hashes": segment_hashes,
            "duration": duration
        }
    
    async def _hash_audio_segment(self, audio_path: Path, start: int, duration: int) -> str:
        """Tạo hash của một đoạn audio"""
        cmd = [
            "ffmpeg", "-ss", str(start), "-t", str(duration),
            "-i", str(audio_path),
            "-f", "wav",
            "-y", "/dev/null"
        ]
        
        # Dùng ffmpeg để lấy raw audio data và hash
        cmd = [
            "ffmpeg", "-ss", str(start), "-t", str(duration),
            "-i", str(audio_path),
            "-f", "s16le", "-acodec", "pcm_s16le",
            "pipe:1"
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        
        return hashlib.md5(stdout).hexdigest()
    
    async def _analyze_audio(self, audio_path: Path) -> Dict:
        """Phân tích thông tin audio"""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,bit_rate:stream=codec_name,sample_rate,channels",
            "-of", "json",
            str(audio_path)
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        
        try:
            info = json.loads(stdout.decode())
            return {
                "duration": float(info.get("format", {}).get("duration", 0)),
                "bitrate": info.get("format", {}).get("bit_rate", "unknown"),
                "format": info.get("format", {}).get("format_name", "unknown")
            }
        except (json.JSONDecodeError, ValueError):
            return {"duration": 0, "bitrate": "unknown", "format": "unknown"}
    
    async def _get_audio_duration(self, audio_path: Path) -> float:
        """Lấy thời lượng audio"""
        info = await self._analyze_audio(audio_path)
        return info.get("duration", 0)
    
    async def _check_local_db(self, fingerprint: Dict) -> List[Dict]:
        """
        Kiểm tra fingerprint với database nội bộ
        Database chứa hash của các bài nhạc/video đã biết có bản quyền
        """
        if not self.fingerprint_db_path.exists():
            return []
        
        try:
            with open(self.fingerprint_db_path, "r") as f:
                db = json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
        
        matches = []
        file_hash = fingerprint.get("file_hash", "")
        
        # So sánh file hash
        for entry in db.get("entries", []):
            if entry.get("hash") == file_hash:
                matches.append({
                    "source": "local_db",
                    "title": entry.get("title", "Unknown"),
                    "artist": entry.get("artist", "Unknown"),
                    "type": entry.get("type", "music"),
                    "confidence": 1.0
                })
        
        # So sánh segment hashes
        for segment in fingerprint.get("segment_hashes", []):
            seg_hash = segment.get("hash", "")
            for entry in db.get("entries", []):
                for db_segment in entry.get("segments", []):
                    if db_segment.get("hash") == seg_hash:
                        match = {
                            "source": "local_db",
                            "title": entry.get("title", "Unknown"),
                            "artist": entry.get("artist", "Unknown"),
                            "type": entry.get("type", "music"),
                            "confidence": 0.8,
                            "matched_at": segment.get("start", 0)
                        }
                        if match not in matches:
                            matches.append(match)
        
        return matches
    
    async def _check_youtube(self, audio_path: Path, audio_info: Dict) -> List[Dict]:
        """
        Kiểm tra với YouTube Data API
        Tìm kiếm bài hát tương tự trên YouTube
        """
        if not self.youtube_api_key:
            return []
        
        # Note: YouTube Content ID API chỉ dành cho partners
        # Ở đây ta dùng Search API để tìm kiếm tương đối
        matches = []
        
        # Tạo query từ audio metadata (nếu có)
        # Hiện tại dùng hash để tìm kiếm
        import aiohttp
        
        # Tìm kiếm video tương tự trên YouTube
        search_url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "q": audio_info.get("title", ""),
            "type": "video",
            "maxResults": 5,
            "key": self.youtube_api_key
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("items", []):
                            matches.append({
                                "source": "youtube",
                                "title": item["snippet"]["title"],
                                "channel": item["snippet"]["channelTitle"],
                                "video_id": item["id"].get("videoId", ""),
                                "confidence": 0.5  # Không chắc chắn
                            })
        except Exception:
            pass
        
        return matches
    
    def _compile_result(self, local_matches: List[Dict], youtube_matches: List[Dict], 
                       audio_info: Dict) -> Dict:
        """Tổng hợp kết quả kiểm tra"""
        all_matches = local_matches + youtube_matches
        
        # Tính toán kết quả
        is_copyrighted = len(local_matches) > 0
        max_confidence = max([m.get("confidence", 0) for m in all_matches], default=0)
        
        warnings = []
        if local_matches:
            warnings.append(f"Phát hiện {len(local_matches)} kết quả khớp với nhạc/video có bản quyền trong database")
        if youtube_matches:
            warnings.append(f"Tìm thấy {len(youtube_matches)} kết quả tương tự trên YouTube (có thể là bản quyền)")
        
        if not all_matches:
            warnings.append("Không tìm thấy kết quả khớp trong database. Tuy nhiên, điều này KHÔNG đảm bảo video/nhạc không có bản quyền.")
        
        return {
            "is_copyrighted": is_copyrighted,
            "confidence": max_confidence,
            "matches": all_matches,
            "warnings": warnings,
            "details": {
                "audio_duration": audio_info.get("duration", 0),
                "local_db_matches": len(local_matches),
                "youtube_matches": len(youtube_matches)
            }
        }
