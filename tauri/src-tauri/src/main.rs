// Steltic desktop shell.
//
// This is the entire native layer: start the Python hub, wait for it to answer, point the
// webview at it. All product logic lives in the hub, which is why swapping this shell for
// Electron, or for nothing at all (a browser tab on the same URL), changes no behaviour.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{net::TcpListener, path::PathBuf, process::{Child, Command, Stdio}, thread, time::{Duration, Instant}};
use tauri::{Manager, WindowEvent};

struct Hub(std::sync::Mutex<Option<Child>>);

fn data_root() -> PathBuf {
    let base = dirs::data_local_dir().unwrap_or_else(|| PathBuf::from("."));
    base.join("Steltic")
}

/// The hub interpreter created by the bootstrap step (see windows/Steltic.ps1 / bootstrap.rs).
fn hub_python() -> PathBuf {
    let env = data_root().join("hubenv");
    if cfg!(windows) { env.join("Scripts").join("pythonw.exe") } else { env.join("bin").join("python") }
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

fn wait_healthy(port: u16, limit: Duration) -> bool {
    let deadline = Instant::now() + limit;
    let url = format!("http://127.0.0.1:{port}/healthz");
    while Instant::now() < deadline {
        if let Ok(r) = ureq::get(&url).timeout(Duration::from_secs(2)).call() {
            if r.status() == 200 { return true; }
        }
        thread::sleep(Duration::from_millis(300));
    }
    false
}

fn main() {
    let port = free_port();
    let data = data_root();
    let py = hub_python();

    // First run (or a wiped data dir): no interpreter yet. The bootstrapper fetches uv,
    // provisions Python and installs the hub, then this path exists. It lives next to the exe.
    if !py.exists() {
        let script = std::env::current_exe().ok()
            .and_then(|p| p.parent().map(|d| d.join("bootstrap").join("Steltic.ps1")))
            .unwrap_or_else(|| PathBuf::from("bootstrap\\Steltic.ps1"));
        let _ = Command::new("powershell")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
            .arg(&script)
            .arg("-BootstrapOnly")
            .env("STELTIC_HUB_DATA", &data)
            .status();
    }

    // The same data root and uv the launcher uses, passed explicitly: otherwise the hub would
    // fall back to its own default and never see the modules the bootstrap installed.
    let uv = data.join("bin").join(if cfg!(windows) { "uv.exe" } else { "uv" });
    let child = Command::new(&py)
        .args(["-m", "steltic_hub.cli", "--port", &port.to_string(), "--no-browser"])
        .env("STELTIC_HUB_DATA", &data)
        .env("STELTIC_HUB_UV", &uv)
        .stdout(Stdio::null()).stderr(Stdio::null())
        .spawn()
        .expect("could not start the Steltic hub");

    if !wait_healthy(port, Duration::from_secs(90)) {
        eprintln!("the Steltic hub did not start; see %LOCALAPPDATA%\\Steltic\\logs");
    }

    tauri::Builder::default()
        .manage(Hub(std::sync::Mutex::new(Some(child))))
        .setup(move |app| {
            let w = app.get_webview_window("main").unwrap();
            w.navigate(format!("http://127.0.0.1:{port}").parse().unwrap())?;
            w.show()?;
            Ok(())
        })
        .on_window_event(move |w, ev| {
            // A module server can be mid-OpenSees run; the hub's own shutdown hook stops them -- but
            // only if the hub gets to run it. kill() is TerminateProcess: no hook, and every module
            // server (and the run inside it) is orphaned. So ask the hub to stop first, the way the
            // launcher does, and kill only what is still there afterwards.
            if let WindowEvent::Destroyed = ev {
                if let Some(state) = w.try_state::<Hub>() {
                    if let Some(mut c) = state.0.lock().unwrap().take() {
                        let _ = ureq::post(&format!("http://127.0.0.1:{port}/api/hub/shutdown"))
                            .timeout(Duration::from_secs(5)).call();
                        let deadline = Instant::now() + Duration::from_secs(20);
                        while Instant::now() < deadline {
                            if let Ok(Some(_)) = c.try_wait() { break; }
                            thread::sleep(Duration::from_millis(200));
                        }
                        let _ = c.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Steltic");
}
