//! VaderAI 데스크톱 껍데기 (Rust 전환 2단계, 2026-11-12).
//!
//! 창·트레이·파일 대화상자·알림·녹화 배지만 여기서. 업무는 전부 파이썬 일꾼(`vader/desktop/worker.py`)이
//! stdin/stdout JSON 줄로 받는다 — 규약은 그 파일 docstring. 화면(assets)은 예전 그대로이고,
//! `window.pywebview.api.call` 모양의 심을 넣어 화면 코드는 바꾸지 않는다.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, DragDropEvent, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tokio::sync::oneshot;

const AUMID: &str = "VaderAI.Desktop";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 화면에 넣는 심. pywebview 와 같은 모양이라 app.js·gate.js·chat.js·messenger.js 를 그대로 쓴다.
const SHIM: &str = r#"(() => {
  if (window.top !== window.self || location.origin !== 'http://tauri.localhost') return;   // 하위 프레임·남의 주소엔 안 넣는다
  const invoke = (m, a) => window.__TAURI_INTERNALS__.invoke('call', { method: String(m), args: a ?? null });
  window.pywebview = { api: { call: invoke } };
  // 파일 끌기는 창(Tauri)이 받아 HTML5 drag 이벤트가 화면에 오지 않는다 — 위치를 받아 그 자리 요소에 같은 이벤트를 다시 쏜다.
  // 그래서 화면의 ondragenter·ondrop(회오리, 놓을 곳 기억)이 그대로 돈다. 경로는 그 뒤 receiveDrop 으로.
  let over = null;
  const fire = (el, type, x, y) => el && el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, dataTransfer: new DataTransfer() }));
  window.__vaderDrag = (kind, x, y, paths) => {
    const el = kind === 'leave' ? null : document.elementFromPoint(x, y);
    if (el !== over) { fire(el, 'dragenter', x, y); fire(over, 'dragleave', x, y); over = el; }
    if (kind === 'over') fire(el, 'dragover', x, y);
    if (kind === 'drop') { fire(el, 'drop', x, y); over = null; window.VaderUI && window.VaderUI.receiveDrop(paths); }
  };
  const ready = () => window.dispatchEvent(new Event('pywebviewready'));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, { once: true }); else ready();
})();"#;

struct Worker {
    /// 보낼 줄 — 쓰는 스레드 하나가 일꾼 stdin 에 쓴다. UI 이벤트가 파이프에서 막히지 않게 (codex1 Rust r3 C)
    /// None 은 쓰는 스레드를 깨우는 신호 — 끊기면(die) 남은 줄을 버리고 stdin 을 닫아 일꾼에게 EOF 를 준다
    out: std::sync::mpsc::SyncSender<Option<String>>,
    /// 일꾼 stdin 파이프 핸들 — 쓰는 스레드가 살아 있는 동안만 Some. 끊기면 막힌 쓰기를 이 핸들로 취소한다.
    /// 쓰는 스레드는 이 잠금 안에서 None 으로 바꾼 뒤에야 핸들을 닫는다 — 취소 중에 닫히지 않는다 (codex1 Rust r6 P1-1·P2-1)
    pipe: Mutex<Option<isize>>,
    next: AtomicU64,
    waiting: Mutex<HashMap<u64, oneshot::Sender<Value>>>,
    exiting: AtomicBool,
    safe: AtomicBool,
    /// 일꾼과의 줄이 끊겼다(쓰기 실패·stdout 닫힘) — 새 호출을 받지 않고, 기다리는 호출은 모두 실패로 끝냈다
    dead: AtomicBool,
}

const GONE: &str = "VaderAI 를 다시 켜 주세요.";

impl Worker {
    /// 한 줄 보낸다(큐에 넣기만). 큐가 차거나 쓰는 스레드가 끝났으면 끊김으로 — 기다리는 호출을 바로 끝낸다
    /// (codex1 Rust r2 P1-4, r3 C).
    fn send(self: &Arc<Self>, msg: Value) -> bool {
        let ok = !self.dead.load(Ordering::SeqCst) && self.out.try_send(Some(msg.to_string())).is_ok();
        if !ok {
            self.die();
        }
        ok
    }

    fn event(self: &Arc<Self>, name: &str) {
        self.send(json!({ "ev": name }));
    }

    /// 줄이 끊겼다 — 기다리는 호출을 실패로 끝내고, 아직 안 보낸 줄은 버리고, stdin 을 닫는다(쓰는 스레드가).
    /// 일꾼은 EOF 를 받아 제 안전 종료 길로 간다 — 실패라고 답한 요청이 나중에 실행되지 않게 (codex1 Rust r4 P1-1).
    fn die(self: &Arc<Self>) {
        let first = !self.dead.swap(true, Ordering::SeqCst);
        for (_, tx) in self.waiting.lock().unwrap().drain() {
            let _ = tx.send(json!({ "ok": false, "error": GONE }));
        }
        let _ = self.out.try_send(None); // 빈 큐에서 기다리는 쓰는 스레드를 깨운다. 차 있으면 다음 줄 전에 dead 를 본다
        // 일꾼이 읽기를 멈춰 쓰기 안에서 막혀 있으면 그 쓰기를 취소한다 — 그래야 stdin 을 닫아 EOF 를 줄 수 있다.
        // 검사 직후·쓰기 직전에 취소가 먼저 지나갈 수 있어 쓰는 스레드가 끝날 때까지 되풀이한다. 이미 넘어간 줄의 결과는 모른다(재시도하지 않는다).
        // 2026-11-12 rustc 1.99.1 실험: 가득 찬 파이프(65KB)에서 막힌 쓰기가 첫 CancelIoEx 에 os error 995 로 풀림
        if first {
            let me = self.clone();
            std::thread::spawn(move || loop {
                {
                    let pipe = me.pipe.lock().unwrap();
                    let Some(h) = *pipe else { break };
                    unsafe {
                        let _ = windows::Win32::System::IO::CancelIoEx(windows::Win32::Foundation::HANDLE(h as _), None);
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            });
        }
    }
}

/// 앱 안 화면인가 — 정확히 http://tauri.localhost (Windows 의 Tauri 자체 프로토콜). 원격 주소에 일꾼을 열지 않는다.
fn ours(url: &tauri::Url) -> bool {
    url.scheme() == "http" && url.host_str() == Some("tauri.localhost") && url.port().is_none()
}

#[tauri::command]
async fn call(
    method: String,
    args: Option<Value>,
    webview: tauri::Webview,
    worker: tauri::State<'_, Arc<Worker>>,
) -> Result<Value, String> {
    // 앱 안 화면, 정해진 창만. 녹화 배지(badge-<번호>)는 끝내기 하나만, 그 창이 만들어질 때의 번호로 (codex1 r1, r5 P2-1).
    let label = webview.label().to_string();
    let badge_rec = label.strip_prefix("badge-").and_then(|n| n.parse::<u64>().ok());
    let allowed = label == "main" || (badge_rec.is_some() && method == "record_end");
    if !allowed || !webview.url().map(|u| ours(&u)).unwrap_or(false) {
        return Err("허용되지 않은 화면".into());
    }
    // 배지의 끝내기는 화면이 보낸 값이 아니라 그 창 라벨의 번호로
    let args = match badge_rec {
        Some(rec) => Some(json!({ "rec": rec })),
        None => args,
    };
    let id = worker.next.fetch_add(1, Ordering::SeqCst);
    let (tx, rx) = oneshot::channel();
    worker.waiting.lock().unwrap().insert(id, tx);
    // 등록한 뒤에 끊김을 본다 — die() 가 그 사이에 돌았어도 이 호출이 남지 않게
    if worker.dead.load(Ordering::SeqCst) || !worker.send(json!({ "id": id, "call": method, "args": args })) {
        worker.waiting.lock().unwrap().remove(&id);
        return Ok(json!({ "ok": false, "error": GONE }));
    }
    Ok(rx.await.unwrap_or_else(|_| json!({ "ok": false, "error": GONE })))
}

fn main() {
    velopack::VelopackApp::build().run(); // 설치·업데이트·제거 때 Velopack 이 부른다 — 여기서 끝날 수 있다
    if relaunched_as_admin() {
        return;
    }
    unsafe {
        let _ = windows::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID(&windows::core::HSTRING::from(AUMID));
    }
    register_aumid();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![call])
        .setup(|app| {
            let worker = spawn_worker(app.handle().clone())?;
            app.manage(worker.clone());
            let main = main_window(app.handle())?;
            // 트레이를 못 만들면 일꾼에게 알린다 — 그때 X 는 숨기지 않고 닫는다 (닫을 길이 없어지지 않게)
            let has_tray = tray(app.handle(), worker.clone()).is_ok();
            worker.send(json!({ "ev": "hello", "tray": has_tray }));
            title_bar(&main);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("VaderAI 껍데기를 만들지 못했습니다")
        .run(|_, event| {
            // 창을 다 닫아도 끝내지 않는다 — 끝은 일꾼이 정한다(exit → 일꾼 종료 → 여기 exit)
            if let tauri::RunEvent::ExitRequested { code: None, api, .. } = event {
                api.prevent_exit();
            }
        });
}

fn main_window(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    // 카톡 PC 처럼 화면 오른쪽에 좁고 긴 창 (마스터 요청 2026-11-09)
    let (sw, sh) = screen(app);
    let (w, h) = (420.0, (sh - 100.0).min(900.0));
    let worker = app.state::<Arc<Worker>>().inner().clone();
    let handle = app.clone();
    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("gate.html".into()))
        .title("VaderAI")
        .inner_size(w, h)
        .min_inner_size(380.0, 560.0)
        .position(sw - w - 24.0, 24.0)
        .background_color(tauri::window::Color(8, 11, 17, 255))
        .initialization_script(SHIM)
        .on_navigation(ours)
        .build()?;
    window.on_window_event(move |event| match event {
        WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close(); // 숨길지·막을지·닫을지는 일꾼이 정한다 (host.close_requested)
            if !worker.exiting.load(Ordering::SeqCst) {
                worker.event("close");
            }
        }
        WindowEvent::DragDrop(ev) => {
            let Some(main) = handle.get_webview_window("main") else { return };
            let scale = main.scale_factor().unwrap_or(1.0);
            let at = |p: &tauri::PhysicalPosition<f64>| {
                let l = p.to_logical::<f64>(scale);
                (l.x, l.y)
            };
            let js = match ev {
                DragDropEvent::Enter { position, .. } | DragDropEvent::Over { position } => {
                    let (x, y) = at(position);
                    format!("window.__vaderDrag&&__vaderDrag('over',{x},{y})")
                }
                DragDropEvent::Drop { paths, position } => {
                    let (x, y) = at(position);
                    let paths: Vec<String> = paths.iter().map(|p| p.display().to_string()).collect();
                    format!("window.__vaderDrag&&__vaderDrag('drop',{x},{y},{})", json!(paths))
                }
                DragDropEvent::Leave => "window.__vaderDrag&&__vaderDrag('leave',0,0)".to_string(),
                _ => return,
            };
            let _ = main.eval(&js);
        }
        _ => {}
    });
    Ok(window)
}

fn screen(app: &AppHandle) -> (f64, f64) {
    app.primary_monitor()
        .ok()
        .flatten()
        .map(|m| {
            let s = m.size().to_logical::<f64>(m.scale_factor());
            (s.width, s.height)
        })
        .unwrap_or((1280.0, 920.0))
}

fn tray(app: &AppHandle, worker: Arc<Worker>) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "VaderAI 열기", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "종료", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    let clicked = worker.clone();
    TrayIconBuilder::with_id("vader")
        .icon(app.default_window_icon().cloned().expect("아이콘"))
        .tooltip("VaderAI")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |_, e| match e.id().as_ref() {
            "open" => worker.event("show"),
            "quit" => worker.event("quit"), // 진행 중인 작업 확인은 일꾼이
            _ => {}
        })
        .on_tray_icon_event(move |_, e| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = e {
                clicked.event("show");
            }
        })
        .build(app)?;
    Ok(())
}

/// 일꾼을 띄우고 stdout 을 읽는 스레드를 단다.
fn spawn_worker(app: AppHandle) -> Result<Arc<Worker>, Box<dyn std::error::Error>> {
    // stderr: 개발 빌드는 이 콘솔로, 설치본은 버린다(업무 내용이 섞일 수 있는 로그를 남기지 않는다 — 오류는 crash 기록이)
    let stderr = if cfg!(debug_assertions) { Stdio::inherit() } else { Stdio::null() };
    let mut child = worker_command()
        .env("VADER_SHELL_PID", std::process::id().to_string()) // 녹화에서 뺄 껍데기 PID — 개발 실행은 부모가 venv 런처라
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(stderr)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()?;
    let (out, lines) = std::sync::mpsc::sync_channel::<Option<String>>(4096);
    let worker = Arc::new(Worker {
        out,
        pipe: Mutex::new(None),
        next: AtomicU64::new(1),
        waiting: Mutex::new(HashMap::new()),
        exiting: AtomicBool::new(false),
        safe: AtomicBool::new(false),
        dead: AtomicBool::new(false),
    });
    let mut stdin: ChildStdin = child.stdin.take().unwrap();
    *worker.pipe.lock().unwrap() = Some(stdin.as_raw_handle() as isize); // 쓰는 스레드를 띄우기 전에 — 취소 수단 없이 쓰지 않는다
    let writer = worker.clone();
    std::thread::spawn(move || {
        while let Ok(Some(line)) = lines.recv() {
            if writer.dead.load(Ordering::SeqCst) {
                break; // 끊긴 뒤의 줄은 보내지 않는다
            }
            if writeln!(stdin, "{line}").and_then(|_| stdin.flush()).is_err() {
                writer.die();
                break;
            }
        }
        *writer.pipe.lock().unwrap() = None; // 취소하던 쪽이 멈춘 뒤에 닫는다
        drop(stdin); // 일꾼에게 EOF
    });
    let stdout = child.stdout.take().unwrap();
    let w = worker.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
            handle(&app, &w, msg);
        }
        // stdout 이 닫혔다 — 기다리는 호출부터 끝내고(자식이 아직 살아 있어도), 그다음 자식을 기다린다
        w.die();
        let exited_ok = child.wait().map(|s| s.success()).unwrap_or(false);
        if !w.exiting.load(Ordering::SeqCst) {
            message("VaderAI 내부 작업자가 멈췄어요. 앱을 다시 켜 주세요.");
        }
        // 일꾼이 안전하게 끝냈다고(작업·콘솔까지) 알리고 정상 종료했을 때만 받아 둔 업데이트를 건다 (codex1 Rust r2 P2-5)
        if w.safe.load(Ordering::SeqCst) && exited_ok {
            apply_pending_update();
        }
        app.exit(0);
    });
    Ok(worker)
}

/// 설치본: 같은 폴더의 vaderai-worker.exe. 개발: 저장소의 .venv-desktop 파이썬 (VADER_PYTHON 으로 바꿈).
fn worker_command() -> Command {
    let exe = std::env::current_exe().unwrap_or_default();
    let bundled = exe.with_file_name("vaderai-worker.exe");
    if bundled.exists() {
        return Command::new(bundled);
    }
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    let python = std::env::var_os("VADER_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| repo.join(".venv-desktop").join("Scripts").join("python.exe"));
    let mut cmd = Command::new(python);
    cmd.args(["-m", "vader.desktop.worker"]).current_dir(repo);
    cmd
}

fn handle(app: &AppHandle, worker: &Arc<Worker>, msg: Value) {
    if let Some(id) = msg.get("id").and_then(Value::as_u64) {
        if let Some(tx) = worker.waiting.lock().unwrap().remove(&id) {
            let _ = tx.send(msg.get("result").cloned().unwrap_or(Value::Null));
        }
        return;
    }
    let Some(ui) = msg.get("ui").and_then(Value::as_str) else { return };
    let main = app.get_webview_window("main");
    let text = |k: &str| msg.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    match (ui, main) {
        ("eval", Some(m)) => {
            let _ = m.eval(&text("js"));
        }
        ("hide", Some(m)) => {
            let _ = m.hide();
        }
        ("show", Some(m)) => {
            let _ = m.show();
            let _ = m.set_focus();
        }
        ("restore", Some(m)) => {
            let _ = m.unminimize();
        }
        ("minimize", Some(m)) => {
            let _ = m.minimize();
        }
        ("exit", main) => {
            worker.exiting.store(true, Ordering::SeqCst);
            if let Some(m) = main {
                let _ = m.hide(); // 일꾼이 뒷정리를 마치고 나가면 위 읽기 스레드가 끝낸다
            }
            close_badges(app);
        }
        ("done", _) => worker.safe.store(msg["safe"].as_bool() == Some(true), Ordering::SeqCst),
        ("notify", _) => {
            // 알림 API 가 받았는지 돌려준다 — 실패면 일꾼이 '알렸음' 으로 치지 않는다 (codex1 Rust r2 P2-4)
            let shown = notify(&text("title"), &text("text"));
            worker.send(json!({ "re": msg["ask"], "result": shown }));
        }
        ("badge_open", _) => {
            let opened = badge(app, worker, msg["rec"].clone());
            worker.send(json!({ "re": msg["ask"], "result": opened }));
        }
        ("badge_close", _) => {
            // 그 녹화의 배지만 — 늦게 온 옛 닫기가 새 배지를 닫지 않게 (codex1 Rust r6 P2-3)
            if let Some(b) = msg["rec"].as_u64().and_then(|n| app.get_webview_window(&format!("badge-{n}"))) {
                let _ = b.destroy();
            }
        }
        ("pick", _) if worker.exiting.load(Ordering::SeqCst) => {
            worker.send(json!({ "re": msg["ask"], "result": [] })); // 닫는 중 — 대화상자를 새로 띄우지 않는다
        }
        ("pick", main) => pick(app, worker.clone(), main, msg),
        _ => {}
    }
}

/// 녹화 중 표시 — 항상 위, 프레임 없음, 오른쪽 위. 닫히면(끝내기·X) 일꾼에게 record_end.
fn close_badges(app: &AppHandle) {
    for (label, w) in app.webview_windows() {
        if label.starts_with("badge-") {
            let _ = w.destroy();
        }
    }
}

fn badge(app: &AppHandle, worker: &Arc<Worker>, rec: Value) -> bool {
    let Some(n) = rec.as_u64() else { return false };
    close_badges(app); // 녹화는 한 번에 하나 — 남은 옛 배지는 닫는다(그 닫힘은 옛 번호로 가서 무시된다)
    let (sw, _) = screen(app);
    let built = WebviewWindowBuilder::new(app, format!("badge-{n}"), WebviewUrl::App("badge.html".into()))
        .title("VaderAI 녹화")
        .inner_size(300.0, 96.0)
        .position(sw - 324.0, 24.0)
        .resizable(false)
        .decorations(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .background_color(tauri::window::Color(12, 15, 21, 255))
        .initialization_script(SHIM)
        .on_navigation(ours)
        .build();
    let Ok(b) = built else { return false };
    let w = worker.clone();
    b.on_window_event(move |e| {
        if let WindowEvent::Destroyed = e {
            // 그 녹화의 번호로 — 늦게 온 옛 배지의 닫힘이 새 녹화를 끝내지 않게 (codex1 Rust r3 E). 답은 버린다(id 0)
            w.send(json!({ "id": 0, "call": "record_end", "args": { "rec": rec.clone() } }));
        }
    });
    true
}

/// 일꾼이 받아 둔 업데이트(Velopack packages)를 이 프로세스가 끝난 뒤 적용하게 건다. 설치본이 아니면 아무것도 안 한다.
fn apply_pending_update() {
    let source = velopack::sources::FileSource::new(std::env::temp_dir());
    let Ok(manager) = velopack::UpdateManager::new(source, None, None) else { return };
    if let Some(asset) = manager.get_update_pending_restart() {
        let _ = manager.wait_exit_then_apply_updates(asset, true, false, Vec::<String>::new());
    }
}

/// 파일·폴더 고르기. 주인 창을 꼭 단다 — 주인 없는 대화상자는 창 뒤로 숨어 앱이 굳은 것처럼 보인다.
fn pick(app: &AppHandle, worker: Arc<Worker>, main: Option<WebviewWindow>, msg: Value) {
    let app = app.clone();
    std::thread::spawn(move || {
        let mut dialog = app.dialog().file();
        if let Some(m) = &main {
            dialog = dialog.set_parent(m);
        }
        for t in msg["types"].as_array().into_iter().flatten().filter_map(Value::as_str) {
            // "설명 (*.zip;*.py)" → 이름 "설명", 확장자 ["zip","py"]
            let (name, pat) = t.rsplit_once('(').unwrap_or((t, ""));
            let exts: Vec<String> = pat
                .trim_end_matches(')')
                .split(';')
                .filter_map(|p| p.trim().strip_prefix("*."))
                .map(|e| if e == "*" { "*".into() } else { e.to_string() })
                .collect();
            let exts: Vec<&str> = exts.iter().map(String::as_str).collect();
            dialog = dialog.add_filter(name.trim(), &exts);
        }
        let paths: Vec<String> = if msg["folder"].as_bool() == Some(true) {
            dialog.blocking_pick_folder().into_iter().collect()
        } else if msg["multiple"].as_bool() == Some(true) {
            dialog.blocking_pick_files().unwrap_or_default()
        } else {
            dialog.blocking_pick_file().into_iter().collect()
        }
        .into_iter()
        .filter_map(|p| p.into_path().ok())
        .map(|p| p.display().to_string())
        .collect();
        worker.send(json!({ "re": msg["ask"], "result": paths }));
    });
}

fn notify(title: &str, text: &str) -> bool {
    tauri_winrt_notification::Toast::new(AUMID).title(title).text1(text).show().is_ok()
}

/// 알림이 'VaderAI' 이름·아이콘으로 뜨게 이 사용자의 앱 ID 를 적는다 (바로가기 없는 앱의 토스트).
fn register_aumid() {
    let Ok((key, _)) = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .create_subkey(format!(r"Software\Classes\AppUserModelId\{AUMID}"))
    else {
        return;
    };
    let _ = key.set_value("DisplayName", &"VaderAI");
    if let Ok(exe) = std::env::current_exe() {
        let icon = exe.with_file_name("_internal").join("vader").join("desktop").join("assets").join("vaderai.png");
        if icon.exists() {
            let _ = key.set_value("IconUri", &icon.display().to_string());
        }
    }
}

/// Windows 11 제목줄을 화면 머리띠 색으로 (#090b0f 바탕, #eef1f5 글자). 그 전 Windows 는 무시한다.
fn title_bar(window: &WebviewWindow) {
    use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWINDOWATTRIBUTE};
    let Ok(raw) = window.hwnd() else { return };
    // Tauri 가 쓰는 windows 판과 이 앱의 판이 달라도 되게 원시 핸들로 넘긴다 (codex1 Rust r2 P1-5)
    let hwnd = windows::Win32::Foundation::HWND(raw.0 as _);
    for (attr, value) in [(20, 1i32), (35, 0x000F_0B09), (36, 0x00F5_F1EE)] {
        unsafe {
            let _ = DwmSetWindowAttribute(hwnd, DWMWINDOWATTRIBUTE(attr), &value as *const i32 as _, 4);
        }
    }
}

/// 설치본은 관리자 권한으로 돈다 (마스터 결정 2026-10-28) — 관리자로 뜬 이온엠에 클릭·키를 보내려면.
/// 일꾼은 이 프로세스의 자식이라 권한을 물려받는다. '아니요' 면 켜지 않는다.
fn relaunched_as_admin() -> bool {
    use windows::core::{HSTRING, PCWSTR};
    use windows::Win32::UI::Shell::{IsUserAnAdmin, ShellExecuteW};
    use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    let exe = std::env::current_exe().unwrap_or_default();
    let installed = exe.parent().and_then(|d| d.parent()).map(|d| d.join("Update.exe").exists()).unwrap_or(false);
    if !installed || unsafe { IsUserAnAdmin() }.as_bool() {
        return false;
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    unsafe {
        ShellExecuteW(
            None,
            &HSTRING::from("runas"),
            &HSTRING::from(exe.as_os_str()),
            &HSTRING::from(args.join(" ")),
            PCWSTR::null(),
            SW_SHOWNORMAL,
        );
    }
    true
}

fn message(text: &str) {
    use windows::core::HSTRING;
    use windows::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONWARNING};
    unsafe {
        MessageBoxW(None, &HSTRING::from(text), &HSTRING::from("VaderAI"), MB_ICONWARNING);
    }
}
