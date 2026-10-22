
import { useEffect, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  BarChart3,
  Clock3,
  Database,
  FileSearch,
  FileText,
  Fingerprint,
  LayoutDashboard,
  Plane,
  Search,
  ShieldCheck,
  Sparkles,
  Upload,
} from "lucide-react";
import "./App.css";

type Page =
  | "overview"
  | "parser"
  | "analytics"
  | "reports"
  | "history"
  | "files";
type SourceFile = {
  name: string;
  path: string;
  size: number;
};
type ParserStatus = {
  status: "idle" | "running" | "completed" | "failed";
  message: string;
  data_dir: string | null;
  log: string;
  output_exists: boolean;
  output_path: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  return_code?: number | null;
};

type AnalysisReport = {
  name: string;
  size: number;
  url: string;
};

type AnalysisStatus = {
  status: "idle" | "running" | "completed" | "failed";
  message: string;
  started_at: string | null;
  finished_at: string | null;
  return_code: number | null;
  log: string;
  reports: AnalysisReport[];
};
type OutputFile = {
  name: string;
  path: string;
  size: number;
  extension: string;
};

type FilePreview = {
  name: string;
  path: string;
  headers?: string[];
  rows?: string[][];
  lines?: string[];
  offset: number;
  limit: number;
  has_more: boolean;
  type: "csv" | "text";
};

const navigation: {
  id: Page;
  label: string;
  icon: typeof LayoutDashboard;
}[] = [
  { id: "overview", label: "Обзор", icon: LayoutDashboard },
  { id: "parser", label: "Парсер данных", icon: FileSearch },
  { id: "analytics", label: "Аналитика", icon: BarChart3 },
  { id: "reports", label: "Отчёты", icon: FileText },
  { id: "history", label: "История запусков", icon: Clock3 },
    { id: "files", label: "Файлы проекта", icon: Database },
];

function App() {
  const [page, setPage] = useState<Page>("overview");
  const [apiStatus, setApiStatus] = useState("Проверяем соединение…");
const [apiReady, setApiReady] = useState(false);
  const [search, setSearch] = useState("");
const API = "http://127.0.0.1:8000";

const [analysisStatus, setAnalysisStatus] =
  useState<AnalysisStatus | null>(null);
const [analysisBusy, setAnalysisBusy] = useState(false);
const [analysisError, setAnalysisError] = useState("");
  const [activeStage, setActiveStage] = useState("stage_1_initial.csv");
  const [stageData, setStageData] = useState<Record<string, { headers: string[]; rows: string[][]; hasMore: boolean; error?: string }>>({});
  const [stageBusy, setStageBusy] = useState(false);
  const [expandedPassenger, setExpandedPassenger] = useState<string | null>(null);
const stageDefinitions = [
  { name: "stage_1_initial.csv", title: "Низкий уровень подозрительности", subtitle: "Проверка паспортных данных, билетов и сведений о пассажирах. Пассажиры хотя бы с одним обнаруженным признаком", number: "01" },
  { name: "stage_2_cross_checked.csv", title: "Средний уровень подозрительности", subtitle: "Анализ маршрутов, частоты перелётов, расписания рейсов и связей между пассажирами", number: "02" },
  { name: "stage_3_spies.csv", title: "Выявленные шпионы", subtitle: "Проверка повторного использования паспортов, выявление несовпадений личных данных и подтверждение подозрительных совпадений", number: "03" },
];

const [dataDir, setDataDir] = useState("");
const [sourceFiles, setSourceFiles] = useState<SourceFile[]>([]);
const [parserStatus, setParserStatus] = useState<ParserStatus | null>(null);
const [busy, setBusy] = useState(false);
const [parserError, setParserError] = useState("");
const [outputFiles, setOutputFiles] = useState<OutputFile[]>([]);
const [selectedFile, setSelectedFile] = useState("");
const [filePreview, setFilePreview] = useState<FilePreview | null>(null);
const [fileOffset, setFileOffset] = useState(0);
const [filesError, setFilesError] = useState("");
const [filesBusy, setFilesBusy] = useState(false);
useEffect(() => {
    fetch("http://127.0.0.1:8000/api/health")
      .then((response) => {
        if (!response.ok) throw new Error("API unavailable");
        return response.json();
      })
      .then((data) => {
        setApiStatus(data.message);
        setApiReady(true);
      })
      .catch(() => {
        setApiStatus("Backend не запущен");
        setApiReady(false);
      });
  }, []);

async function chooseFolder() {
  setBusy(true);
  setParserError("");

  try {
    const response = await fetch(`${API}/api/parser/choose-folder`, {
      method: "POST",
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Не удалось выбрать файлы");
    }

    if (data.cancelled) return;

    const newFiles: SourceFile[] = data.files || [];

    setSourceFiles((previous) => {
      const uniqueFiles = new Map(
        previous.map((file) => [file.path, file])
      );

      for (const file of newFiles) {
        uniqueFiles.set(file.path, file);
      }

      return Array.from(uniqueFiles.values());
    });

    setDataDir(data.data_dir || "");
  } catch (error) {
    setParserError(
      error instanceof Error ? error.message : "Ошибка выбора файлов"
    );
  } finally {
    setBusy(false);
  }
}

async function startParser() {
  setBusy(true);
  setParserError("");

  try {
    const response = await fetch(`${API}/api/parser/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_dir: dataDir }),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Не удалось запустить парсер");
    }

    await refreshParserStatus();
  } catch (error) {
    setParserError(
      error instanceof Error ? error.message : "Ошибка запуска парсера"
    );
  } finally {
    setBusy(false);
  }
}

async function refreshParserStatus() {
  try {
    const response = await fetch(`${API}/api/parser/status`);
    if (!response.ok) throw new Error("Не удалось получить статус");

    const data: ParserStatus = await response.json();
    setParserStatus(data);

    if (data.data_dir && !dataDir) {
      setDataDir(data.data_dir);
    }
  } catch {
    // Ошибка временного запроса не ломает страницу.
  }
}

async function refreshOutputFiles() {
  try {
    const response = await fetch(`${API}/api/files`);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Не удалось загрузить файлы");
    }

    setOutputFiles(data.files || []);
  } catch (error) {
    setFilesError(
      error instanceof Error ? error.message : "Ошибка загрузки файлов"
    );
  }
}

async function openOutputFile(path: string, offset = 0) {
  setFilesBusy(true);
  setFilesError("");
  setSelectedFile(path);
  setFileOffset(offset);

  try {
    const params = new URLSearchParams({
      path,
      offset: String(offset),
      limit: "50",
    });

    const response = await fetch(`${API}/api/files/preview?${params}`);
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Не удалось открыть файл");
    }

    setFilePreview(data);
  } catch (error) {
    setFilePreview(null);
    setFilesError(
      error instanceof Error ? error.message : "Ошибка просмотра файла"
    );
  } finally {
    setFilesBusy(false);
  }
}
useEffect(() => {
  refreshOutputFiles();

  const timer = window.setInterval(refreshOutputFiles, 5000);
  return () => window.clearInterval(timer);
}, []);

async function refreshAnalysisStatus() {
  try {
    const response = await fetch(`${API}/api/analysis/status`);
    if (!response.ok) throw new Error("Не удалось получить статус анализа");

    const data: AnalysisStatus = await response.json();
    setAnalysisStatus(data);
  } catch {
    // Повторим запрос при следующем обновлении.
  }
}

async function loadStage(name: string) {
  setActiveStage(name);
  setStageBusy(true);
  setExpandedPassenger(null);
  try {
    const params = new URLSearchParams({ path: `analysis_results/${name}`, offset: "0", limit: "200" });
    const response = await fetch(`${API}/api/files/preview?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Не удалось загрузить этап");
    setStageData((old) => ({ ...old, [name]: { headers: data.headers || [], rows: data.rows || [], hasMore: Boolean(data.has_more) } }));
  } catch (error) {
    setStageData((old) => ({ ...old, [name]: { headers: [], rows: [], hasMore: false, error: error instanceof Error ? error.message : "Ошибка загрузки" } }));
  } finally { setStageBusy(false); }
}

async function startAnalysis() {
  setAnalysisBusy(true);
  setAnalysisError("");

  try {
    const response = await fetch(`${API}/api/analysis/run`, {
      method: "POST",
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Не удалось запустить анализ");
    }

    await refreshAnalysisStatus();
  } catch (error) {
    setAnalysisError(
      error instanceof Error ? error.message : "Ошибка запуска анализа"
    );
  } finally {
    setAnalysisBusy(false);
  }
}

function formatSize(bytes: number) {
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(0)} КБ`;
  }

  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

useEffect(() => {
  refreshParserStatus();

  const timer = window.setInterval(() => {
    refreshParserStatus();
  }, 2000);

  return () => window.clearInterval(timer);
}, []);

useEffect(() => {
  refreshAnalysisStatus();

  const timer = window.setInterval(() => {
    refreshAnalysisStatus();
  }, 2000);

  return () => window.clearInterval(timer);
}, []);

  const active = navigation.find((item) => item.id === page);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => setPage("overview")}>
          <span className="brand-mark"><Plane size={23} /></span>
          <span className="brand-copy">
            <strong>FlightWatch</strong>
            <small>FLIGHT DATA INTELLIGENCE</small>
          </span>
        </button>

        <div className="sidebar-caption">РАБОЧЕЕ ПРОСТРАНСТВО</div>

        <nav className="navigation">
          {navigation.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={`nav-item ${page === id ? "active" : ""}`}
              onClick={() => setPage(id)}
            >
              <Icon size={19} strokeWidth={1.8} />
              <span>{label}</span>
              {page === id && <span className="nav-indicator" />}
            </button>
          ))}
        </nav>

        <div className="sidebar-bottom">
          <div className="privacy-icon"><ShieldCheck size={20} /></div>
          <strong>Локальная обработка</strong>
          <p>Данные остаются на вашем компьютере и не отправляются в облако.</p>
          <div className={`connection ${apiReady ? "online" : "offline"}`}>
            <span />
            {apiReady ? "API подключён" : "API ожидает запуска"}
          </div>
        </div>

        <div className="sidebar-footer">FLIGHTWATCH · LOCAL EDITION</div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="breadcrumb">FlightWatch <span>/</span> {active?.label}</div>
          <div className="local-badge"><span /> Локальная среда</div>
        </header>

        {page === "overview" && (
          <section className="page-content overview-page">
            <div className="welcome-row">
              <div>
                <div className="eyebrow"><Sparkles size={14} /> DATA WORKSPACE</div>
                <p className="subtitle">
                  Обрабатывайте данные о рейсах, исследуйте несоответствия
                  и формируйте отчёты в одном месте.
                </p>
              </div>
              <div className="hero-emblem"><Plane size={37} strokeWidth={1.2} /></div>
            </div>


            <div className="section-heading">
              <div><h2>Рабочее пространство</h2></div>
            </div>

            <div className="workspace-grid">
              <button className="workspace-card parser-card" onClick={() => setPage("parser")}>
                <div className="card-top">
                  <span className="card-icon pink"><FileSearch size={22} /></span>
                  <ArrowRight size={19} className="card-arrow" />
                </div>
                <span className="card-label">01 / DATA PROCESSING</span>
                <h3>Парсер данных</h3>
                <p>Подготовка и объединение исходных файлов в единый набор данных.</p>
                <div className="card-bottom"><Database size={15} /> Обработка файлов</div>
              </button>

              <button className="workspace-card analytics-card" onClick={() => setPage("analytics")}>
                <div className="card-top">
                  <span className="card-icon lilac"><BarChart3 size={22} /></span>
                  <ArrowRight size={19} className="card-arrow" />
                </div>
                <span className="card-label">02 / DATA ANALYSIS</span>
                <h3>Аналитика</h3>
                <p>Поиск несоответствий в документах, расписании и данных пассажиров.</p>
                <div className="card-bottom"><Fingerprint size={15} /> Проверка записей</div>
              </button>

              <button className="workspace-card reports-card" onClick={() => setPage("reports")}>
                <div className="card-top">
                  <span className="card-icon peach"><FileText size={22} /></span>
                  <ArrowRight size={19} className="card-arrow" />
                </div>
                <span className="card-label">03 / EXPORT</span>
                <h3>Отчёты</h3>
                <p>Результаты запусков, сводки и выгрузка обработанных данных.</p>
                <div className="card-bottom"><ArrowDownToLine size={15} /> Экспорт результатов</div>
              </button>
            </div>

            <div className="lower-grid">
              <section className="panel">
                <div className="panel-heading">
                  <div><h2>Последние действия</h2><p>Активность рабочего пространства</p></div>
                  <Clock3 size={19} />
                </div>
                <div className="activity-list">
                  <div className="activity-row"><span className="activity-mark"><FileSearch size={16}/></span><div><strong>Парсер данных</strong><small>{parserStatus?.started_at ? `Запуск: ${parserStatus.started_at}` : "Запусков с известным временем нет"}</small></div><span className={`state-tag ${parserStatus?.status === "completed" ? "good" : parserStatus?.status === "failed" ? "bad" : ""}`}>{parserStatus?.status || "idle"}</span></div>
                  <div className="activity-row"><span className="activity-mark"><BarChart3 size={16}/></span><div><strong>Аналитика</strong><small>{analysisStatus?.started_at ? `Запуск: ${analysisStatus.started_at}` : "Запусков с известным временем нет"}</small></div><span className={`state-tag ${analysisStatus?.status === "completed" ? "good" : analysisStatus?.status === "failed" ? "bad" : ""}`}>{analysisStatus?.status || "idle"}</span></div>
                  <button className="text-button" onClick={() => setPage("history")}>Открыть историю <ArrowRight size={15}/></button>
                </div>
              </section>

              <section className="panel quick-panel">
                <div className="panel-heading">
                  <div><h2>Быстрый поиск</h2><p>Поиск в подготовленных данных</p></div>
                  <Search size={19} />
                </div>
                <form className="search-box" onSubmit={(event) => {
                  event.preventDefault();
                  setPage("parser");
                }}>
                  <Search size={17} />
                  <input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Пассажир, документ или рейс…"
                  />
                  <button type="submit" aria-label="Найти"><ArrowRight size={17} /></button>
                </form>
                <div className="tip-box">
                  <Sparkles size={17} />
                  <p>Начни с обработки исходных файлов, чтобы подготовить данные для анализа.</p>
                </div>
              </section>
            </div>
          </section>
        )}


{page === "parser" && (
  <section className="page-content">
    <div className="eyebrow">
      <FileSearch size={14} /> DATA PROCESSING
    </div>

    <h1>Парсер данных<span className="pink-dot">.</span></h1>
    <p className="subtitle">
      Выбери исходные данные и следи за обработкой в одном месте.
    </p>

    <div className="panel parser-workspace">
      <span className="large-icon"><Upload size={27} /></span>
      <h2>Исходные данные</h2>
      <p>Выбери папку с файлами на компьютере.</p>

      <button
        className="primary-button"
        onClick={chooseFolder}
        disabled={busy || parserStatus?.status === "running"}
      >
        <Upload size={17} />
        {busy ? "Подожди..." : "Выбрать папку"}
      </button>

      {dataDir && (
        <div className="tip-box">
          <Database size={17} />
          <p><strong>Выбранная папка</strong><br />{dataDir}</p>
        </div>
      )}

      {sourceFiles.length > 0 && (
        <div style={{ marginTop: 20 }}>
          <h2>Файлы в папке</h2>
          <p>{sourceFiles.length} файлов</p>
          {sourceFiles.map((file) => (
            <div
              key={file.path}
              className="card-bottom"
              style={{
                justifyContent: "space-between",
                padding: "10px 0",
                gap: 16,
              }}
            >
<span style={{ overflowWrap: "anywhere" }}>
  <strong>{file.name}</strong>
  <br />
  <small>{file.path}</small>
</span>
                <span>{formatSize(file.size)}</span>
            </div>
          ))}
        </div>
      )}

      <button
        className="primary-button"
        onClick={startParser}
        disabled={!dataDir || busy || parserStatus?.status === "running"}
      >
        <FileSearch size={17} />
        Запустить обработку
      </button>

      {parserError && <div className="tip-box"><p>{parserError}</p></div>}
    </div>

    <div className="panel" style={{ marginTop: 20 }}>
      <div className="panel-heading">
        <div>
          <h2>Состояние обработки</h2>
          <p>Обновляется автоматически</p>
        </div>
        <Activity size={19} />
      </div>

      <div className="status-banner">
        <div className="status-symbol"><Activity size={21} /></div>
        <div className="status-copy">
          <strong>{parserStatus?.message || "Получаем состояние..."}</strong>
          <span>
            {parserStatus?.started_at
              ? `Начало: ${parserStatus.started_at}`
              : "Ожидание запуска"}
          </span>
        </div>
        <span className={`status-pill ${
          parserStatus?.status === "completed" ? "success" : ""
        }`}>
          {(parserStatus?.status || "loading").toUpperCase()}
        </span>
      </div>


      {parserStatus?.log && (
        <details style={{ marginTop: 16 }}>
          <summary>Журнал обработки</summary>
          <pre style={{
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            maxHeight: 360,
            overflowY: "auto",
            fontSize: 12,
          }}>
            {parserStatus.log}
          </pre>
        </details>
      )}
    </div>
  </section>
)}



{page === "analytics" && (
  <section className="page-content">
    <div className="eyebrow"><BarChart3 size={14} /> DATA ANALYSIS</div>
    <h1>Аналитика<span className="pink-dot">.</span></h1>
    <p className="subtitle">Последовательность отбора по трём CSV-файлам, сформированным алгоритмом анализа.</p>
    <div className="panel parser-workspace">
      <span className="large-icon"><Fingerprint size={27} /></span>
      <h2>Анализ пассажиров и рейсов</h2>
      <p>Этапы загружаются из workspace/processed/analysis_results. Сайт не пересчитывает правила анализа.</p>
      <button className="primary-button" onClick={startAnalysis} disabled={analysisBusy || analysisStatus?.status === "running" || parserStatus?.status === "running" || !parserStatus?.output_exists}>
        <BarChart3 size={17} />{analysisBusy || analysisStatus?.status === "running" ? "Анализ выполняется..." : "Запустить анализ"}
      </button>
      {!parserStatus?.output_exists && <div className="tip-box"><Database size={17} /><p>Сначала необходимо подготовить данные в разделе «Парсер данных».</p></div>}
      {analysisError && <div className="tip-box"><p>{analysisError}</p></div>}
    </div>
    <div className="stage-track">
      {stageDefinitions.map((stage) => {
        const loaded = stageData[stage.name];
        return <button key={stage.name} className={`stage-step ${activeStage === stage.name ? "active" : ""}`} onClick={() => loadStage(stage.name)}>
          <span className="stage-number">{stage.number}</span><span className="stage-step-copy"><strong>{stage.title}</strong><small>{stage.subtitle}</small></span>
          <span className="stage-count">{loaded ? `${loaded.rows.length}${loaded.hasMore ? "+" : ""} в предпросмотре` : "Открыть этап"}</span>
        </button>;
      })}
    </div>
    <div className="panel stage-panel">
      <div className="panel-heading"><div><h2>{stageDefinitions.find((x) => x.name === activeStage)?.title}</h2><p>{activeStage}</p></div><button className="text-button" onClick={() => loadStage(activeStage)} disabled={stageBusy}>{stageBusy ? "Загружаем..." : "Обновить"}</button></div>
      <p className="stage-description">{stageDefinitions.find((x) => x.name === activeStage)?.subtitle}</p>
      {!stageData[activeStage] && !stageBusy && <div className="empty-state"><span className="empty-icon"><FileSearch size={22} /></span><strong>Этап ещё не загружен</strong><p>Открой этап, чтобы прочитать соответствующий CSV-файл.</p><button className="text-button" onClick={() => loadStage(activeStage)}>Загрузить данные <ArrowRight size={15} /></button></div>}
      {stageBusy && <p className="subtitle">Загружаем CSV-файл...</p>}
      {stageData[activeStage]?.error && <div className="tip-box"><p>{stageData[activeStage].error}. Проверь, что анализ завершён и CSV-файл существует.</p></div>}
      {stageData[activeStage] && !stageData[activeStage].error && <>
        <div className="stage-summary"><div><strong>{stageData[activeStage].rows.length}{stageData[activeStage].hasMore ? "+" : ""}</strong><span>строк в предпросмотре</span></div><div><strong>{stageData[activeStage].headers.length}</strong><span>полей в CSV</span></div><div><strong>{stageData[activeStage].hasMore ? "Есть ещё" : "Конец файла"}</strong><span>состояние предпросмотра</span></div></div>
        <p className="stage-description">Показаны первые 200 строк максимум. Полное количество пассажиров текущий API не сообщает, поэтому число строк предпросмотра не выдаётся за полный размер выборки.</p>
        {stageData[activeStage].rows.length === 0 ? <div className="empty-state"><strong>В файле нет записей</strong><p>CSV прочитан, но строки данных отсутствуют.</p></div> : <div className="stage-table-wrap"><table className="stage-table"><thead><tr><th>Подробности</th>{stageData[activeStage].headers.map((h,i)=><th key={`${h}-${i}`}>{h}</th>)}</tr></thead><tbody>
          {stageData[activeStage].rows.map((row, ri) => { const idIndex = stageData[activeStage].headers.findIndex((h) => ["person_id","passenger_id","entity_id","id"].includes(h.toLowerCase())); const pid = idIndex >= 0 ? (row[idIndex] || `Строка ${ri+1}`) : `Строка ${ri+1}`; const key = `${activeStage}-${pid}-${ri}`; return <>{<tr key={key}><td><button className="text-button" onClick={() => setExpandedPassenger(expandedPassenger === key ? null : key)}>{expandedPassenger === key ? "Скрыть" : "Открыть"}</button></td>{stageData[activeStage].headers.map((_,ci)=><td key={ci}>{row[ci] ?? ""}</td>)}</tr>}{expandedPassenger === key && <tr key={`${key}-detail`}><td colSpan={stageData[activeStage].headers.length+1}><div className="passenger-details"><strong>Подробности записи: {pid}</strong>{stageData[activeStage].headers.map((h,ci)=><div key={`${h}-${ci}`}><span>{h}</span><p>{row[ci] || "В CSV нет значения"}</p></div>)}</div></td></tr>}</>; })}
        </tbody></table></div>}
        <a className="text-button stage-download" href={`${API}/api/files/download?path=${encodeURIComponent(`analysis_results/${activeStage}`)}`}>Скачать полный CSV <ArrowDownToLine size={15} /></a>
      </>}
    </div>
  </section>
)}

        {page === "reports" && (
          <section className="page-content">
            <div className="eyebrow"><FileText size={14} /> REPORTS & EXPORT</div>
            <h1>Отчёты<span className="pink-dot">.</span></h1>
            <p className="subtitle">Скачивай реальные результаты парсера и анализа. Файлы берутся из локального workspace/processed.</p>
            <div className="metric-grid">
              <div className="metric-card"><span className="metric-icon"><Database size={19} /></span><span className="metric-label">ОБРАБОТАННЫЙ ДАТАСЕТ</span><strong>{parserStatus?.output_exists ? "Готов" : "Не создан"}</strong><small>{parserStatus?.output_exists ? "flights_clean.csv доступен" : "Сначала запусти парсер"}</small></div>
              <div className="metric-card"><span className="metric-icon"><BarChart3 size={19} /></span><span className="metric-label">ОТЧЁТЫ АНАЛИЗА</span><strong>{analysisStatus?.reports.length ?? 0}</strong><small>CSV-файлов найдено backend</small></div>
              <div className="metric-card"><span className="metric-icon"><FileText size={19} /></span><span className="metric-label">ФАЙЛЫ РЕЗУЛЬТАТОВ</span><strong>{outputFiles.length}</strong><small>Файлов в обработанной папке</small></div>
            </div>
            <div className="panel report-panel">
              <div className="panel-heading"><div><h2>Результаты анализа</h2><p>{analysisStatus?.message || "Статус анализа ещё загружается"}</p></div><button className="text-button" onClick={refreshAnalysisStatus}>Обновить</button></div>
              {analysisStatus?.reports.length ? <div className="data-table-wrap"><table className="data-table"><thead><tr><th>Имя отчёта</th><th>Размер</th><th>Формат</th><th>Действие</th></tr></thead><tbody>{analysisStatus.reports.map((report) => <tr key={report.name}><td><span className="file-name"><FileText size={16} />{report.name}</span></td><td>{formatSize(report.size)}</td><td><span className="soft-tag">CSV</span></td><td><a className="table-action" href={`${API}/api/files/download?path=${encodeURIComponent(`analysis_results/${report.name}`)}`}><ArrowDownToLine size={15} /> Скачать</a></td></tr>)}</tbody></table></div> : <div className="empty-state"><span className="empty-icon"><FileText size={22} /></span><strong>Отчёты пока не найдены</strong><p>Запусти анализ в разделе «Аналитика». Когда backend создаст CSV, они появятся здесь.</p><button className="text-button" onClick={() => setPage("analytics")}>Перейти к аналитике <ArrowRight size={15} /></button></div>}
            </div>
            <div className="panel report-panel">
              <div className="panel-heading"><div><h2>Файлы обработки</h2><p>Последние доступные файлы в workspace/processed</p></div><button className="text-button" onClick={refreshOutputFiles}>Обновить</button></div>
              {outputFiles.filter((file) => !file.path.startsWith("analysis_results/")).length ? <div className="data-table-wrap"><table className="data-table"><thead><tr><th>Файл</th><th>Размер</th><th>Тип</th><th>Действие</th></tr></thead><tbody>{outputFiles.filter((file) => !file.path.startsWith("analysis_results/")).map((file) => <tr key={file.path}><td><span className="file-name"><FileText size={16} />{file.name}</span></td><td>{formatSize(file.size)}</td><td><span className="soft-tag">{file.extension.replace(".", "").toUpperCase() || "FILE"}</span></td><td><a className="table-action" href={`${API}/api/files/download?path=${encodeURIComponent(file.path)}`}><ArrowDownToLine size={15} /> Скачать</a></td></tr>)}</tbody></table></div> : <p className="muted-copy">Пока нет файлов обработки.</p>}
            </div>
          </section>
        )}

{page === "files" && (
  <section className="page-content">
    <div className="eyebrow">
      <Database size={14} /> PROJECT FILES
    </div>

    <h1>Файлы проекта<span className="pink-dot">.</span></h1>
    <p className="subtitle">
      Подготовленные данные, результаты аналитики и журналы выполнения.
    </p>

    <div className="panel">
      <div className="panel-heading">
        <div>
          <h2>Выходные файлы</h2>
          <p>{outputFiles.length} файлов в workspace/processed</p>
        </div>
        <button
          className="text-button"
          onClick={refreshOutputFiles}
        >
          Обновить
        </button>
      </div>

      {filesError && <p>{filesError}</p>}

      {outputFiles.map((file) => (
        <div
          key={file.path}
          className="card-bottom"
          style={{
            justifyContent: "space-between",
            gap: 16,
            padding: "12px 0",
          }}
        >
          <div style={{ minWidth: 0, overflowWrap: "anywhere" }}>
            <strong>{file.name}</strong>
            <br />
            <small>{file.path} · {formatSize(file.size)}</small>
          </div>

          <div style={{ display: "flex", gap: 12, flexShrink: 0 }}>
            <button
              className="text-button"
              onClick={() => openOutputFile(file.path)}
              disabled={filesBusy}
            >
              Просмотр
            </button>

            <a
              className="text-button"
              href={`${API}/api/files/download?path=${encodeURIComponent(file.path)}`}
            >
              Скачать <ArrowDownToLine size={15} />
            </a>
          </div>
        </div>
      ))}
    </div>

    {filePreview && (
      <div className="panel" style={{ marginTop: 20 }}>
        <div className="panel-heading">
          <div>
            <h2>{filePreview.name}</h2>
            <p>
              {filePreview.type === "csv"
                ? "Табличный просмотр"
                : "Текстовый просмотр"}
            </p>
          </div>
          <FileText size={19} />
        </div>

        {filesBusy && <p>Загружаем данные...</p>}

        {filePreview.type === "csv" && filePreview.headers && (
          <div style={{ overflowX: "auto", maxWidth: "100%" }}>
            <table style={{ borderCollapse: "collapse", minWidth: "100%" }}>
              <thead>
                <tr>
                  {filePreview.headers.map((header, index) => (
                    <th
                      key={`${header}-${index}`}
                      style={{
                        textAlign: "left",
                        padding: 10,
                        borderBottom: "1px solid #eadde2",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filePreview.rows?.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {filePreview.headers!.map((_, columnIndex) => (
                      <td
                        key={columnIndex}
                        style={{
                          padding: 10,
                          borderBottom: "1px solid #f0e7eb",
                          verticalAlign: "top",
                          maxWidth: 320,
                          overflowWrap: "anywhere",
                        }}
                      >
                        {row[columnIndex] ?? ""}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {filePreview.type === "text" && (
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {filePreview.lines?.join("\n")}
          </pre>
        )}

        {filePreview.type === "csv" && (
          <div style={{ display: "flex", gap: 12, marginTop: 16 }}>
            <button
              className="text-button"
              disabled={fileOffset === 0 || filesBusy}
              onClick={() =>
                openOutputFile(
                  selectedFile,
                  Math.max(0, fileOffset - 50)
                )
              }
            >
              Предыдущие
            </button>

            <span>Строки с {fileOffset + 1}</span>

            <button
              className="text-button"
              disabled={!filePreview.has_more || filesBusy}
              onClick={() =>
                openOutputFile(selectedFile, fileOffset + 50)
              }
            >
              Следующие
            </button>
          </div>
        )}
      </div>
    )}
  </section>
)}

        {page === "history" && (
          <section className="page-content">
            <div className="eyebrow"><Clock3 size={14} /> RUN HISTORY</div>
            <h1>История запусков<span className="pink-dot">.</span></h1>
            <p className="subtitle">Последние известные состояния парсера и анализа. Текущий API хранит состояние только в памяти backend, поэтому это не архив всех прошлых запусков.</p>
            <div className="metric-grid">
              <div className="metric-card"><span className="metric-icon"><FileSearch size={19} /></span><span className="metric-label">ПАРСЕР</span><strong>{parserStatus?.status === "completed" ? "Завершён" : parserStatus?.status === "running" ? "Выполняется" : parserStatus?.status === "failed" ? "Ошибка" : "Ожидание"}</strong><small>{parserStatus?.started_at ? `Начало: ${parserStatus.started_at}` : "Время запуска не указано"}</small></div>
              <div className="metric-card"><span className="metric-icon"><BarChart3 size={19} /></span><span className="metric-label">АНАЛИЗ</span><strong>{analysisStatus?.status === "completed" ? "Завершён" : analysisStatus?.status === "running" ? "Выполняется" : analysisStatus?.status === "failed" ? "Ошибка" : "Ожидание"}</strong><small>{analysisStatus?.started_at ? `Начало: ${analysisStatus.started_at}` : "Время запуска не указано"}</small></div>
            </div>
            <div className="panel report-panel"><div className="panel-heading"><div><h2>Последние запуски</h2><p>Данные, которые backend сообщает сейчас</p></div><button className="text-button" onClick={() => { refreshParserStatus(); refreshAnalysisStatus(); }}>Обновить</button></div>
              <div className="data-table-wrap"><table className="data-table"><thead><tr><th>Процесс</th><th>Статус</th><th>Начало</th><th>Завершение</th><th>Результат</th></tr></thead><tbody>
                <tr><td><span className="file-name"><FileSearch size={16} />Парсер</span></td><td><span className={`state-tag ${parserStatus?.status === "completed" ? "good" : parserStatus?.status === "failed" ? "bad" : ""}`}>{parserStatus?.status || "unknown"}</span></td><td>{parserStatus?.started_at || "—"}</td><td>{parserStatus?.finished_at || "—"}</td><td>{parserStatus?.output_exists ? "flights_clean.csv доступен" : parserStatus?.message || "—"}</td></tr>
                <tr><td><span className="file-name"><BarChart3 size={16} />Анализ</span></td><td><span className={`state-tag ${analysisStatus?.status === "completed" ? "good" : analysisStatus?.status === "failed" ? "bad" : ""}`}>{analysisStatus?.status || "unknown"}</span></td><td>{analysisStatus?.started_at || "—"}</td><td>{analysisStatus?.finished_at || "—"}</td><td>{analysisStatus?.reports.length ? `${analysisStatus.reports.length} CSV-отчётов` : analysisStatus?.message || "—"}</td></tr>
              </tbody></table></div>
            </div>
          </section>
        )}

        <footer className="page-footer">
          <span>FlightWatch <span className="pink-dot">●</span> Local Edition</span>
          <span>Локальная обработка данных</span>
        </footer>
      </main>
    </div>
  );
}

export default App;
