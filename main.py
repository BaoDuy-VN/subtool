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


# ===== VIDEO EDITOR API =====

@app.post("/api/translate-burn")
async def translate_and_burn(
    file: UploadFile = File(...),
    srt_file: UploadFile = File(None),
    font_size: int = 24,
    background_tasks: BackgroundTasks = None
):
    """
    Dịch phụ đề Trung → Việt và burn vào video
    1. Nếu có srt_file: dịch và burn trực tiếp
    2. Nếu không có srt_file: tách sub bằng Whisper → dịch → burn
    """
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"translate-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    # Save video
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    # Save SRT if provided
    srt_path = None
    if srt_file and srt_file.filename:
        srt_path = job_dir / srt_file.filename
        with open(srt_path, "wb") as f:
            shutil.copyfileobj(srt_file.file, f)
    
    if background_tasks:
        background_tasks.add_task(process_translate_burn, job_id, video_path, srt_path, font_size)
    
    return {"job_id": job_id, "status": "processing", "message": "Đang xử lý..."}


@app.get("/api/translate-burn/{job_id}/status")
async def check_translate_status(job_id: str):
    """Kiểm tra trạng thái dịch + burn sub"""
    job_dir = UPLOAD_DIR / f"translate-{job_id}"
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job không tồn tại")
    
    # Check output video - chỉ completed khi có done.txt (ffmpeg đã xong hẳn)
    output_videos = list(job_dir.glob("output_*.mp4"))
    if output_videos and (job_dir / "done.txt").exists():
        return {
            "status": "completed",
            "job_id": job_id,
            "output_file": f"/static/uploads/translate-{job_id}/{output_videos[0].name}",
            "filename": output_videos[0].name
        }
    
    error_file = job_dir / "error.txt"
    if error_file.exists():
        return {"status": "error", "message": error_file.read_text()}
    
    # Check progress file
    progress_file = job_dir / "progress.txt"
    if progress_file.exists():
        return {"status": "processing", "job_id": job_id, "progress": progress_file.read_text()}
    
    return {"status": "processing", "job_id": job_id}


@app.get("/api/translate-burn/{job_id}/download")
async def download_translated(job_id: str):
    """Tải video đã dịch + burn sub"""
    job_dir = UPLOAD_DIR / f"translate-{job_id}"
    output_videos = list(job_dir.glob("output_*.mp4"))
    if not output_videos or not (job_dir / "done.txt").exists():
        raise HTTPException(status_code=404, detail="Chưa có video output")
    return FileResponse(output_videos[0], filename=output_videos[0].name)


@app.get("/api/translate-burn/{job_id}/srt")
async def download_translated_srt(job_id: str):
    """Tải file SRT đã dịch"""
    job_dir = UPLOAD_DIR / f"translate-{job_id}"
    srt_files = list(job_dir.glob("*_vi.srt"))
    if not srt_files:
        raise HTTPException(status_code=404, detail="Chưa có SRT đã dịch")
    return FileResponse(srt_files[0], filename=srt_files[0].name, media_type="text/plain")


@app.post("/api/edit/trim")
async def trim_video(
    file: UploadFile = File(...),
    start_time: float = 0,
    end_time: float = None,
    background_tasks: BackgroundTasks = None
):
    """Cắt video theo thời gian"""
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    if background_tasks:
        background_tasks.add_task(process_trim, job_id, video_path, start_time, end_time)
    
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/edit/speed")
async def change_speed(
    file: UploadFile = File(...),
    speed: float = 1.0,
    background_tasks: BackgroundTasks = None
):
    """Điều chỉnh tốc độ video"""
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    if background_tasks:
        background_tasks.add_task(process_speed, job_id, video_path, speed)
    
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/edit/compress")
async def compress_video(
    file: UploadFile = File(...),
    crf: int = 28,
    background_tasks: BackgroundTasks = None
):
    """Nén video giảm dung lượng"""
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    if background_tasks:
        background_tasks.add_task(process_compress, job_id, video_path, crf)
    
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/edit/extract-audio")
async def extract_audio_api(
    file: UploadFile = File(...),
    format: str = "mp3",
    background_tasks: BackgroundTasks = None
):
    """Tách audio từ video"""
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    if background_tasks:
        background_tasks.add_task(process_extract_audio, job_id, video_path, format)
    
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/edit/resize")
async def resize_video_api(
    file: UploadFile = File(...),
    width: int = None,
    height: int = None,
    background_tasks: BackgroundTasks = None
):
    """Resize video"""
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    if background_tasks:
        background_tasks.add_task(process_resize, job_id, video_path, width, height)
    
    return {"job_id": job_id, "status": "processing"}


@app.post("/api/edit/watermark")
async def add_watermark_api(
    file: UploadFile = File(...),
    text: str = "SubTool",
    position: str = "bottom_right",
    background_tasks: BackgroundTasks = None
):
    """Thêm watermark vào video"""
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    job_dir.mkdir(exist_ok=True)
    
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    if background_tasks:
        background_tasks.add_task(process_watermark, job_id, video_path, text, position)
    
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/edit/{job_id}/status")
async def check_edit_status(job_id: str):
    """Kiểm tra trạng thái chỉnh sửa"""
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job không tồn tại")
    
    # Check output files
    output_files = list(job_dir.glob("output_*"))
    if output_files:
        return {
            "status": "completed",
            "job_id": job_id,
            "output_file": f"/static/uploads/edit-{job_id}/{output_files[0].name}",
            "filename": output_files[0].name
        }
    
    error_file = job_dir / "error.txt"
    if error_file.exists():
        return {"status": "error", "message": error_file.read_text()}
    
    return {"status": "processing", "job_id": job_id}


@app.get("/api/edit/{job_id}/download")
async def download_edited(job_id: str):
    """Tải file đã chỉnh sửa"""
    job_dir = UPLOAD_DIR / f"edit-{job_id}"
    output_files = list(job_dir.glob("output_*"))
    if not output_files:
        raise HTTPException(status_code=404, detail="Chưa có file output")
    return FileResponse(output_files[0], filename=output_files[0].name)


# ===== BACKGROUND PROCESSING FOR EDITOR =====

async def process_trim(job_id: str, video_path: Path, start_time: float, end_time: float):
    from services.video_editor import VideoEditor
    try:
        editor = VideoEditor()
        output_path = video_path.parent / f"output_trimmed{video_path.suffix}"
        await editor.trim_video(video_path, output_path, start_time, end_time)
    except Exception as e:
        (UPLOAD_DIR / f"edit-{job_id}" / "error.txt").write_text(str(e))


async def process_speed(job_id: str, video_path: Path, speed: float):
    from services.video_editor import VideoEditor
    try:
        editor = VideoEditor()
        output_path = video_path.parent / f"output_speed{video_path.suffix}"
        await editor.change_speed(video_path, output_path, speed)
    except Exception as e:
        (UPLOAD_DIR / f"edit-{job_id}" / "error.txt").write_text(str(e))


async def process_compress(job_id: str, video_path: Path, crf: int):
    from services.video_editor import VideoEditor
    try:
        editor = VideoEditor()
        output_path = video_path.parent / f"output_compressed.mp4"
        await editor.compress_video(video_path, output_path, crf=crf)
    except Exception as e:
        (UPLOAD_DIR / f"edit-{job_id}" / "error.txt").write_text(str(e))


async def process_extract_audio(job_id: str, video_path: Path, format: str):
    from services.video_editor import VideoEditor
    try:
        editor = VideoEditor()
        output_path = video_path.parent / f"output_audio.{format}"
        await editor.extract_audio(video_path, output_path, format=format)
    except Exception as e:
        (UPLOAD_DIR / f"edit-{job_id}" / "error.txt").write_text(str(e))


async def process_resize(job_id: str, video_path: Path, width: int, height: int):
    from services.video_editor import VideoEditor
    try:
        editor = VideoEditor()
        output_path = video_path.parent / f"output_resized{video_path.suffix}"
        await editor.resize_video(video_path, output_path, width=width, height=height)
    except Exception as e:
        (UPLOAD_DIR / f"edit-{job_id}" / "error.txt").write_text(str(e))


async def process_watermark(job_id: str, video_path: Path, text: str, position: str):
    from services.video_editor import VideoEditor
    try:
        editor = VideoEditor()
        output_path = video_path.parent / f"output_watermark{video_path.suffix}"
        await editor.add_watermark(video_path, output_path, text=text, position=position)
    except Exception as e:
        (UPLOAD_DIR / f"edit-{job_id}" / "error.txt").write_text(str(e))


async def process_translate_burn(job_id: str, video_path: Path, srt_path: Path, font_size: int):
    """Xử lý dịch phụ đề và burn vào video"""
    from services.translation_service import TranslationService
    from services.video_editor import VideoEditor
    
    job_dir = UPLOAD_DIR / f"translate-{job_id}"
    progress_file = job_dir / "progress.txt"
    
    try:
        translator = TranslationService()
        editor = VideoEditor()
        
        # Step 1: Nếu không có SRT, tách sub bằng Whisper
        if not srt_path or not srt_path.exists():
            progress_file.write_text("Đang tách phụ đề bằng Whisper AI...")
            from services.subtitle_extractor import SubtitleExtractor
            extractor = SubtitleExtractor()
            srt_path = await extractor.extract(video_path, job_id)
        
        # Step 2: Dịch SRT
        progress_file.write_text("Đang dịch phụ đề Trung → Việt...")
        translated_srt = await translator.translate_srt(srt_path)
        
        # Step 3: Burn sub vào video
        progress_file.write_text("Đang gắn phụ đề vào video...")
        output_path = job_dir / f"output_translated.mp4"
        await editor.burn_subtitle(
            video_path=video_path,
            srt_path=translated_srt,
            output_path=output_path,
            font_size=font_size,
            position="bottom"
        )
        
        # Done - đánh dấu hoàn thành để status endpoint báo completed
        (job_dir / "done.txt").write_text("ok")
        if progress_file.exists():
            progress_file.unlink()
            
    except Exception as e:
        (job_dir / "error.txt").write_text(str(e))
        raise


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
