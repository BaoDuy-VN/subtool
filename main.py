"""
SubTool - Tách hard sub & Kiểm tra bản quyền video/nhạc
Kết hợp 2 tool: OCR subtitle extraction + Copyright checker
"""
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
import shutil
import os
import uuid
import asyncio
from pathlib import Path

# Create app
app = FastAPI(title="SubTool - Tách Hard Sub & Kiểm Tra Bản Quyền")

# Static files & templates
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Upload directory
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Max file size: 500MB
MAX_FILE_SIZE = 500 * 1024 * 1024


@app.get("/")
async def homepage():
    """Trang chủ - hiển thị giao diện chính"""
    html_path = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/extract-sub")
async def extract_subtitle(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    """
    API tách hard sub từ video
    1. Lưu video vào thư mục tạm
    2. Dùng FFmpeg cắt frames
    3. OCR để nhận diện phụ đề
    4. Xuất file SRT
    """
    # Validate file type (by extension, more reliable than content-type)
    allowed_extensions = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Định dạng video không hỗ trợ: {file_ext}. Hỗ trợ: {', '.join(allowed_extensions)}")
    
    # Generate unique ID for this job
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    
    # Save uploaded file
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    # Check file size
    file_size = video_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        shutil.rmtree(job_dir)
        raise HTTPException(status_code=413, detail="File quá lớn (tối đa 500MB)")
    
    # Start background processing
    if background_tasks:
        background_tasks.add_task(process_subtitle_extraction, job_id, video_path)
    
    return {"job_id": job_id, "filename": file.filename, "status": "processing"}


@app.get("/api/extract-sub/{job_id}/status")
async def check_extraction_status(job_id: str):
    """Kiểm tra trạng thái xử lý"""
    job_dir = UPLOAD_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job không tồn tại")
    
    # Check if SRT file exists (extraction complete)
    srt_files = list(job_dir.glob("*.srt"))
    if srt_files:
        return {
            "status": "completed",
            "job_id": job_id,
            "srt_file": f"/static/uploads/{job_id}/{srt_files[0].name}",
            "subtitles_found": count_srt_entries(srt_files[0])
        }
    
    # Check if error file exists
    error_file = job_dir / "error.txt"
    if error_file.exists():
        return {"status": "error", "message": error_file.read_text()}
    
    return {"status": "processing", "job_id": job_id}


@app.get("/api/download/{job_id}")
async def download_subtitle(job_id: str):
    """Tải file SRT đã xử lý"""
    job_dir = UPLOAD_DIR / job_id
    srt_files = list(job_dir.glob("*.srt"))
    if not srt_files:
        raise HTTPException(status_code=404, detail="Chưa có file SRT")
    return FileResponse(srt_files[0], filename=srt_files[0].name, media_type="text/plain")


@app.post("/api/check-copyright")
async def check_copyright(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    """
    API kiểm tra bản quyền video/nhạc
    1. Lưu file vào thư mục tạm
    2. Trích xuất audio
    3. Tạo audio fingerprint
    4. So sánh với database (YouTube API / local DB)
    """
    # Validate file type (by extension)
    allowed_extensions = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".ogg"}
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Định dạng không hỗ trợ: {file_ext}")
    
    # Generate unique ID
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"copyright-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    # Save file
    file_path = job_dir / file.filename
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    # Start background processing
    if background_tasks:
        background_tasks.add_task(process_copyright_check, job_id, file_path)
    
    return {"job_id": job_id, "filename": file.filename, "status": "checking"}


@app.get("/api/check-copyright/{job_id}/status")
async def check_copyright_status(job_id: str):
    """Kiểm tra trạng thái kiểm tra bản quyền"""
    job_dir = UPLOAD_DIR / f"copyright-{job_id}"
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job không tồn tại")
    
    result_file = job_dir / "result.json"
    if result_file.exists():
        import json
        result = json.loads(result_file.read_text())
        return {"status": "completed", **result}
    
    error_file = job_dir / "error.txt"
    if error_file.exists():
        return {"status": "error", "message": error_file.read_text()}
    
    return {"status": "checking", "job_id": job_id}


def count_srt_entries(srt_path: Path) -> int:
    """Đếm số dòng sub trong file SRT"""
    content = srt_path.read_text(encoding="utf-8")
    return content.count("\n\n") + 1


# ===== BACKGROUND PROCESSING =====

async def process_subtitle_extraction(job_id: str, video_path: Path):
    """Xử lý tách sub trong background"""
    from services.subtitle_extractor import SubtitleExtractor
    
    try:
        extractor = SubtitleExtractor()
        srt_path = await extractor.extract(video_path, job_id)
        return srt_path
    except Exception as e:
        error_file = UPLOAD_DIR / job_id / "error.txt"
        error_file.write_text(str(e))
        raise


async def process_copyright_check(job_id: str, file_path: Path):
    """Xử lý kiểm tra bản quyền trong background"""
    from services.copyright_checker import CopyrightChecker
    
    try:
        checker = CopyrightChecker()
        result = await checker.check(file_path, job_id)
        
        import json
        result_file = UPLOAD_DIR / f"copyright-{job_id}" / "result.json"
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception as e:
        error_file = UPLOAD_DIR / f"copyright-{job_id}" / "error.txt"
        error_file.write_text(str(e))
        raise


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
