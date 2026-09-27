//! VFR Navlog desktop shell.
//!
//! * `navlog` runs one request through the Python package (`python -m vfr_navlog.api`),
//!   so route resolution, leg math and PDF export stay in one place.
//! * `ofm://` serves openflightmaps chart tiles from the same disk cache the PDF
//!   renderer uses (`~/.cache/vfr-navlog/ofm/{cycle}/{layer}/{z}/{x}/{y}.{ext}`),
//!   fetching and caching on a miss — once a route is cached it works offline.

use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};

use serde_json::Value;
use tauri::http::{Response, StatusCode};

const TILE_API: &str =
    "https://nwy-tiles-api.prod.newaydata.com/tiles/{z}/{x}/{y}.{ext}?path={cycle}/{layer}/latest";
const USER_AGENT: &str = "vfr-navlog-app/0.1 (+local VFR planning tool)";

/// The vfr-navlog checkout: `VFR_NAVLOG_ROOT`, else two levels above this crate (app/src-tauri).
fn navlog_root() -> PathBuf {
    std::env::var_os("VFR_NAVLOG_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join(".."))
}

fn python_exe(root: &PathBuf) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    }
}

fn run_python(req: Value) -> Result<Value, String> {
    let root = navlog_root();
    let python = python_exe(&root);
    if !python.exists() {
        return Err(format!("Python venv not found at {}", python.display()));
    }
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "vfr_navlog.api"])
        .current_dir(&root)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd.spawn().map_err(|e| format!("failed to start Python: {e}"))?;
    child
        .stdin
        .take()
        .ok_or("no stdin")?
        .write_all(req.to_string().as_bytes())
        .map_err(|e| e.to_string())?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    let resp: Value = serde_json::from_slice(&out.stdout).map_err(|_| {
        format!(
            "Python returned no JSON (exit {:?}): {}",
            out.status.code(),
            String::from_utf8_lossy(&out.stderr).lines().last().unwrap_or("")
        )
    })?;
    if resp.get("ok").and_then(Value::as_bool) == Some(true) {
        Ok(resp)
    } else {
        Err(resp
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown error")
            .to_string())
    }
}

#[tauri::command]
async fn navlog(req: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || run_python(req))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn load_plan(path: String) -> Result<String, String> {
    std::fs::read_to_string(&path).map_err(|e| format!("{path}: {e}"))
}

#[tauri::command]
fn save_plan(path: String, content: String) -> Result<(), String> {
    std::fs::write(&path, content).map_err(|e| format!("{path}: {e}"))
}

// --- Chart tiles ------------------------------------------------------------

struct TileKey {
    cycle: String,
    layer: String,
    z: u32,
    x: u32,
    y: u32,
}

impl TileKey {
    /// `/{cycle}/{layer}/{z}/{x}/{y}` — every part validated, so the cache path is safe.
    fn parse(path: &str) -> Option<Self> {
        let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
        if parts.len() != 5 || parts[0].len() != 4 || !parts[0].bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        if parts[1] != "base" && parts[1] != "aero" {
            return None;
        }
        Some(Self {
            cycle: parts[0].to_string(),
            layer: parts[1].to_string(),
            z: parts[2].parse().ok()?,
            x: parts[3].parse().ok()?,
            y: parts[4].parse().ok()?,
        })
    }

    fn ext(&self) -> &'static str {
        if self.layer == "base" { "jpg" } else { "png" }
    }

    fn cache_path(&self) -> Option<PathBuf> {
        let home = dirs::home_dir()?;
        Some(
            home.join(".cache").join("vfr-navlog").join("ofm").join(&self.cycle).join(&self.layer)
                .join(self.z.to_string()).join(self.x.to_string())
                .join(format!("{}.{}", self.y, self.ext())),
        )
    }

    fn url(&self) -> String {
        TILE_API
            .replace("{z}", &self.z.to_string())
            .replace("{x}", &self.x.to_string())
            .replace("{y}", &self.y.to_string())
            .replace("{ext}", self.ext())
            .replace("{cycle}", &self.cycle)
            .replace("{layer}", &self.layer)
    }
}

async fn fetch_tile(client: &reqwest::Client, key: &TileKey) -> Option<Vec<u8>> {
    let path = key.cache_path()?;
    if let Ok(bytes) = tokio::fs::read(&path).await {
        return Some(bytes);
    }
    let resp = client.get(key.url()).send().await.ok()?;
    if resp.status() != reqwest::StatusCode::OK {
        return None;
    }
    let bytes = resp.bytes().await.ok()?.to_vec();
    if let Some(dir) = path.parent() {
        let _ = tokio::fs::create_dir_all(dir).await;
        let _ = tokio::fs::write(&path, &bytes).await;
    }
    Some(bytes)
}

fn tile_response(status: StatusCode, content_type: &str, body: Vec<u8>) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header("Content-Type", content_type)
        .header("Access-Control-Allow-Origin", "*")
        .body(body)
        .unwrap()
}

// --- Airport charts -----------------------------------------------------------

/// `/{ICAO}/{file}.png` inside `~/.cache/vfr-navlog/charts` (filled by the Python
/// side). Both parts are validated so the protocol can serve nothing else.
fn chart_path(path: &str) -> Option<PathBuf> {
    let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
    if parts.len() != 2 {
        return None;
    }
    let (icao, file) = (parts[0], parts[1]);
    let icao_ok = icao.len() == 4 && icao.bytes().all(|b| b.is_ascii_uppercase());
    let stem = file.strip_suffix(".png")?;
    let file_ok = !stem.is_empty() && stem.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_');
    if !icao_ok || !file_ok {
        return None;
    }
    Some(dirs::home_dir()?.join(".cache").join("vfr-navlog").join("charts").join(icao).join(file))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let client = reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .expect("http client");

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .register_asynchronous_uri_scheme_protocol("ofm", move |_ctx, request, responder| {
            let client = client.clone();
            let path = request.uri().path().to_string();
            tauri::async_runtime::spawn(async move {
                let resp = match TileKey::parse(&path) {
                    None => tile_response(StatusCode::BAD_REQUEST, "text/plain", b"bad tile path".to_vec()),
                    Some(key) => match fetch_tile(&client, &key).await {
                        Some(bytes) => {
                            let ct = if key.layer == "base" { "image/jpeg" } else { "image/png" };
                            tile_response(StatusCode::OK, ct, bytes)
                        }
                        None => tile_response(StatusCode::NOT_FOUND, "text/plain", Vec::new()),
                    },
                };
                responder.respond(resp);
            });
        })
        .register_asynchronous_uri_scheme_protocol("chart", |_ctx, request, responder| {
            let path = request.uri().path().to_string();
            tauri::async_runtime::spawn(async move {
                let resp = match chart_path(&path) {
                    None => tile_response(StatusCode::BAD_REQUEST, "text/plain", b"bad chart path".to_vec()),
                    Some(p) => match tokio::fs::read(&p).await {
                        Ok(bytes) => tile_response(StatusCode::OK, "image/png", bytes),
                        Err(_) => tile_response(StatusCode::NOT_FOUND, "text/plain", Vec::new()),
                    },
                };
                responder.respond(resp);
            });
        })
        .invoke_handler(tauri::generate_handler![navlog, load_plan, save_plan])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
