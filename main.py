
from pathlib import Path
from threading import Lock, Thread
import subprocess
import sys
import time
import csv
from fastapi.responses import FileResponse
import tkinter as tk
from tkinter import filedialog

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
PARSER_DIR = ROOT / "algorithms" / "parser"
RAW_DIR = ROOT / "workspace" / "raw" / "Airlines"
PASSES_DIR = ROOT / "workspace" / "raw" / "boarding_passes"
OUTPUT_DIR = ROOT / "workspace" / "processed"
OUTPUT_CSV = OUTPUT_DIR / "flights_clean.csv"
ANALYSIS_SCRIPT = ROOT / "algorithms" / "analysis" / "find_suspicious.py"
ANALYSIS_DIR = OUTPUT_DIR / "analysis_results"
app = FastAPI(title="FlightWatch API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Запрещает запускать два экземпляра парсера одновременно.
parser_run_lock = Lock()

# Защищает состояние парсера, не блокируя запросы на время обработки.
state_lock = Lock()

parser_state = {
    "status": "idle",
    "message": "Парсер ожидает запуска",
    "data_dir": None,
    "started_at": None,
    "finished_at": None,
    "return_code": None,
    "log": "",
}

analysis_run_lock = Lock()
analysis_state_lock = Lock()

analysis_state = {
    "status": "idle",
    "message": "Анализ ещё не запускался",
    "started_at": None,
    "finished_at": None,
    "return_code": None,
    "log": "",
}

class ParserRequest(BaseModel):
    data_dir: str | None = None


def run_parser(data_dir: Path):
    log_path = OUTPUT_DIR / "parser_run.log"

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        command = [
            sys.executable,
            "-m", "flightparser",
            "--data-dir", str(data_dir),
            "--passes-dir", str(PASSES_DIR),
            "--out", str(OUTPUT_CSV),
        ]

        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=str(PARSER_DIR),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            return_code = process.wait()

        log_text = log_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        with state_lock:
            parser_state.update({
                "status": "completed" if return_code == 0 else "failed",
                "message": (
                    "Обработка завершена"
                    if return_code == 0
                    else f"Парсер завершился с кодом {return_code}"
                ),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "return_code": return_code,
                "log": log_text[-12000:],
            })

    except Exception as exc:
        with state_lock:
            parser_state.update({
                "status": "failed",
                "message": str(exc),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "log": str(exc),
            })

    finally:
        parser_run_lock.release()


def run_analysis():
    log_path = OUTPUT_DIR / "analysis_run.log"

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

        command = [
            sys.executable,
            str(ANALYSIS_SCRIPT),
            "--input", str(OUTPUT_CSV),
            "--outdir", str(ANALYSIS_DIR),
        ]

        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            return_code = process.wait()

        log_text = log_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        with analysis_state_lock:
            analysis_state.update({
                "status": "completed" if return_code == 0 else "failed",
                "message": (
                    "Анализ завершён"
                    if return_code == 0
                    else f"Анализ завершился с кодом {return_code}"
                ),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "return_code": return_code,
                "log": log_text[-12000:],
            })

    except Exception as exc:
        with analysis_state_lock:
            analysis_state.update({
                "status": "failed",
                "message": str(exc),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "log": str(exc),
            })

    finally:
        analysis_run_lock.release()

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "message": "FlightWatch backend is running",
    }


@app.get("/api/overview")
def overview():
    return {
        "processed_datasets": int(OUTPUT_CSV.exists()),
        "analysis_runs": 0,
        "status": "ready",
    }


@app.post("/api/parser/choose-folder")
def choose_folder():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    try:
        selected = filedialog.askopenfilenames(
            title="Выберите исходные файлы",
            initialdir=str(RAW_DIR),
            filetypes=[
                ("Все поддерживаемые файлы", "*.csv *.json *.xml *.tab *.yaml *.yml *.pdf *.zip"),
                ("Все файлы", "*.*"),
            ],
        )
    finally:
        root.destroy()

    if not selected:
        return {
            "cancelled": True,
            "files": [],
            "data_dir": None,
        }

    files = [
        {
            "name": Path(file).name,
            "path": str(Path(file).resolve()),
            "size": Path(file).stat().st_size,
        }
        for file in selected
        if Path(file).is_file()
    ]

    return {
        "cancelled": False,
        "files": files,
        "file_count": len(files),
        "data_dir": str(RAW_DIR ),
    }
@app.post("/api/parser/run")
def start_parser(request: ParserRequest):
    data_dir = (
        Path(request.data_dir).expanduser()
        if request.data_dir
        else RAW_DIR
    )

    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir


    data_dir = data_dir.resolve()

    # Исправляем путь, если Airlines случайно добавился дважды.
    if data_dir == RAW_DIR / "Airlines":
        data_dir = RAW_DIR

    if not data_dir.is_dir():

        raise HTTPException(
            status_code=400,
            detail=f"Папка с исходными данными не найдена: {data_dir}",
        )

    if not PASSES_DIR.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Папка посадочных талонов не найдена: {PASSES_DIR}",
        )

    if not parser_run_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Парсер уже выполняется",
        )

    with state_lock:
        parser_state.update({
            "status": "running",
            "message": "Парсер обрабатывает данные",
            "data_dir": str(data_dir),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "return_code": None,
            "log": "",
        })

    try:
        thread = Thread(
            target=run_parser,
            args=(data_dir,),
            daemon=True,
        )
        thread.start()

    except Exception as exc:
        parser_run_lock.release()

        with state_lock:
            parser_state.update({
                "status": "failed",
                "message": str(exc),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "log": str(exc),
            })

        raise HTTPException(
            status_code=500,
            detail=f"Не удалось запустить парсер: {exc}",
        ) from exc

    return {
        "status": "running",
        "message": "Парсер запущен",
        "data_dir": str(data_dir),
    }


@app.get("/api/parser/status")
def parser_status():
    with state_lock:
        result = dict(parser_state)

    result["output_exists"] = OUTPUT_CSV.exists()
    result["output_path"] = (
        str(OUTPUT_CSV) if OUTPUT_CSV.exists() else None
    )

    return result

@app.post("/api/analysis/run")
def start_analysis():
    if not OUTPUT_CSV.is_file():
        raise HTTPException(
            status_code=400,
            detail="Сначала необходимо завершить обработку исходных данных",
        )

    with state_lock:
        if parser_state["status"] == "running":
            raise HTTPException(
                status_code=409,
                detail="Дождитесь завершения парсера",
            )

    if not ANALYSIS_SCRIPT.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"Скрипт анализа не найден: {ANALYSIS_SCRIPT}",
        )

    if not analysis_run_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Анализ уже выполняется",
        )

    with analysis_state_lock:
        analysis_state.update({
            "status": "running",
            "message": "Выполняется анализ пассажиров и рейсов",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "return_code": None,
            "log": "",
        })

    try:
        Thread(target=run_analysis, daemon=True).start()
    except Exception as exc:
        analysis_run_lock.release()
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось запустить анализ: {exc}",
        ) from exc

    return {
        "status": "running",
        "message": "Анализ запущен",
    }


@app.get("/api/analysis/status")
def get_analysis_status():
    with analysis_state_lock:
        result = dict(analysis_state)

    result["reports"] = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "url": f"/api/analysis/reports/{path.name}",
        }
        for path in sorted(ANALYSIS_DIR.glob("*.csv"))
        if path.is_file()
    ] if ANALYSIS_DIR.is_dir() else []

    return result


@app.get("/api/analysis/reports/{filename}")
def get_analysis_report(filename: str):
    allowed_reports = {
        "findings.csv",
        "spies.csv",
        "suspicious_high.csv",
        "suspicious_low.csv",
        "associates.csv",
        "cotravel_pairs.csv",
        "timetable_coverage.csv",
        "summary.csv",
    "stage_1_initial.csv",
    "stage_2_cross_checked.csv",
    "stage_3_spies.csv",
    }

    if filename not in allowed_reports:
        raise HTTPException(status_code=404, detail="Отчёт не найден")

    report_path = ANALYSIS_DIR / filename

    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="Отчёт ещё не создан")

    from fastapi.responses import FileResponse

    return FileResponse(
        path=report_path,
        media_type="text/csv",
        filename=filename,
    )

def resolve_output_file(relative_path: str) -> Path:
    candidate = (OUTPUT_DIR / relative_path).resolve()

    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Недопустимый путь")

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return candidate


@app.get("/api/files")
def list_output_files():
    if not OUTPUT_DIR.is_dir():
        return {"files": []}

    files = []

    for path in sorted(OUTPUT_DIR.rglob("*")):
        if not path.is_file():
            continue

        relative_path = path.relative_to(OUTPUT_DIR).as_posix()

        files.append({
            "name": path.name,
            "path": relative_path,
            "size": path.stat().st_size,
            "extension": path.suffix.lower(),
        })

    return {"files": files}


@app.get("/api/files/preview")
def preview_output_file(
    path: str,
    offset: int = 0,
    limit: int = 100,
):
    file_path = resolve_output_file(path)

    if offset < 0 or limit < 1:
        raise HTTPException(status_code=400, detail="Некорректные параметры")

    limit = min(limit, 200)

    if file_path.suffix.lower() == ".csv":
        try:
            with file_path.open(
                "r", encoding="utf-8-sig", errors="replace", newline=""
            ) as file:
                sample = file.read(8192)
                file.seek(0)

                try:
                    dialect = csv.Sniffer().sniff(
                        sample, delimiters=";,\t|"
                    )
                    delimiter = dialect.delimiter
                except csv.Error:
                    delimiter = ";"

                reader = csv.reader(file, delimiter=delimiter)
                headers = next(reader, [])
                skipped = 0

                while skipped < offset:
                    if next(reader, None) is None:
                        break
                    skipped += 1

                rows = []
                for _ in range(limit + 1):
                    row = next(reader, None)
                    if row is None:
                        break
                    rows.append(row)

                has_more = len(rows) > limit
                rows = rows[:limit]

            return {
                "name": file_path.name,
                "path": path,
                "headers": headers,
                "rows": rows,
                "offset": offset,
                "limit": limit,
                "has_more": has_more,
                "type": "csv",
            }

        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Не удалось прочитать CSV: {exc}"
            ) from exc

    if file_path.suffix.lower() in {".txt", ".log", ".json", ".yaml", ".yml"}:
        try:
            with file_path.open(
                "r", encoding="utf-8-sig", errors="replace"
            ) as file:
                lines = []
                for _ in range(offset):
                    if file.readline() == "":
                        break

                for _ in range(limit):
                    line = file.readline()
                    if line == "":
                        break
                    lines.append(line.rstrip("\r\n"))

                has_more = file.readline() != ""

            return {
                "name": file_path.name,
                "path": path,
                "lines": lines,
                "offset": offset,
                "limit": limit,
                "has_more": has_more,
                "type": "text",
            }

        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Не удалось прочитать файл: {exc}"
            ) from exc

    raise HTTPException(
        status_code=415,
        detail="Предпросмотр этого типа файла не поддерживается. Его можно скачать.",
    )


@app.get("/api/files/download")
def download_output_file(path: str):
    file_path = resolve_output_file(path)

    return FileResponse(
        path=file_path,
        filename=file_path.name,
    )


