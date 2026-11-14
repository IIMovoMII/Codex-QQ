#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bridge;
mod initial_window;
#[cfg(not(debug_assertions))]
mod integrity;
mod owned_job;
mod profile;
mod updates;
mod window_state;
use bridge::ServiceBridge;
use std::sync::{Arc, Mutex};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent};

#[derive(Clone)]
struct BridgeState(Arc<Mutex<ServiceBridge>>);
struct TrayStatus {
    runtime: MenuItem<tauri::Wry>,
    startup: MenuItem<tauri::Wry>,
}
static INITIAL_WINDOW: initial_window::InitialWindow = initial_window::InitialWindow::new();

fn login_launch() -> bool {
    std::env::args().any(|s| s == "--startup")
}

#[tauri::command]
async fn app_request(
    state: State<'_, BridgeState>,
    request: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let bridge = state.0.clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge
            .lock()
            .map_err(|_| "本机服务锁不可用")?
            .request(request)
    })
    .await
    .map_err(|_| "本机服务请求未完成")?
}

#[tauri::command]
async fn app_snapshot(
    app: AppHandle,
    state: State<'_, BridgeState>,
) -> Result<serde_json::Value, String> {
    let bridge = state.0.clone();
    let tray_app = app.clone();
    let result: Result<serde_json::Value, String> =
        tauri::async_runtime::spawn_blocking(move || {
            let request = serde_json::json!({"command":"get_snapshot", "params":{}});
            let response = bridge
                .lock()
                .map_err(|_| "本机服务锁不可用")?
                .request(request)?;
            if response.get("status").and_then(|value| value.as_str()) != Some("confirmed") {
                return Err("本机状态尚未确认".into());
            }
            let data = response
                .get("data")
                .cloned()
                .unwrap_or(serde_json::Value::Null);
            if let Some(status) = tray_app.try_state::<TrayStatus>() {
                let text = match data["snapshot"]["runtime"].as_str() {
                    Some("ready") => "运行中",
                    Some("stopped") => "已停止",
                    _ => "待核对",
                };
                let _ = status.runtime.set_text(format!("助手最近状态：{text}"));
                let text = if data["snapshot"]["startupVerified"] == true {
                    "已验证登录启动"
                } else if data["snapshot"]["startupEnabled"] == true {
                    "已配置，待登录验证"
                } else {
                    "未启用或待核对"
                };
                let _ = status.startup.set_text(format!("Windows 自启：{text}"));
            }
            Ok(data)
        })
        .await
        .map_err(|_| "本机状态请求未完成")?;
    match &result {
        Ok(data) => {
            let _ = app_appearance(
                app.clone(),
                data["preferences"]["theme"]
                    .as_str()
                    .unwrap_or("light")
                    .into(),
            );
            if INITIAL_WINDOW.snapshot(
                data["snapshot"]["configured"].as_bool().unwrap_or(false),
                data["snapshot"]["migrationRequired"]
                    .as_bool()
                    .unwrap_or(true),
                login_launch(),
                data["preferences"]["startupView"] == "tray",
            ) {
                show_main(&app);
            }
        }
        Err(_) if INITIAL_WINDOW.failed(login_launch()) => show_main(&app),
        _ => {}
    }
    result
}

#[tauri::command]
async fn app_exit(app: AppHandle, state: State<'_, BridgeState>) -> Result<(), String> {
    save_window(&app);
    let bridge = state.0.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut bridge = bridge.lock().map_err(|_| "本机服务锁不可用")?;
        bridge.shutdown();
        Ok::<(), String>(())
    })
    .await
    .map_err(|_| "助手退出操作未完成")??;
    app.exit(0);
    Ok(())
}

#[tauri::command]
fn app_reset_window(app: AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        window_state::fit(&window.as_ref().window(), false);
        window_state::schedule_save(&window.as_ref().window());
    }
}

#[tauri::command]
fn app_appearance(app: AppHandle, theme: String) -> Result<(), String> {
    let value = match theme.as_str() {
        "light" => Some(tauri::Theme::Light),
        "dark" => Some(tauri::Theme::Dark),
        "system" => None,
        _ => return Err("appearance_invalid".into()),
    };
    if let Some(window) = app.get_webview_window("main") {
        window
            .set_theme(value)
            .map_err(|_| "appearance_unavailable")?;
        paint_background(
            &window,
            value.unwrap_or_else(|| window.theme().unwrap_or(tauri::Theme::Light)),
        )?;
    }
    Ok(())
}

fn paint_background(window: &tauri::WebviewWindow, theme: tauri::Theme) -> Result<(), String> {
    // Opaque host and WebView must cover newly exposed pixels during live resize.
    // Keep these colors in sync with --page in tokens.css; do not combine an
    // opaque client area with a forced dark DWM/Mica backdrop.
    let color = if theme == tauri::Theme::Dark {
        tauri::window::Color(21, 23, 27, 255)
    } else {
        tauri::window::Color(244, 245, 247, 255)
    };
    window
        .set_background_color(Some(color))
        .map_err(|_| "appearance_unavailable".into())
}

fn open_logs() {
    if let Some(data) = profile::data() {
        let logs = data.join("logs");
        let _ = std::fs::create_dir_all(&logs);
        let _ = std::process::Command::new("explorer.exe").arg(logs).spawn();
    }
}

fn native_failure() {
    #[cfg(windows)]
    unsafe {
        #[link(name = "user32")]
        unsafe extern "system" {
            fn MessageBoxW(window: isize, text: *const u16, title: *const u16, flags: u32) -> i32;
        }
        let text:Vec<u16>="无法打开助手界面。请使用中文安装包修复安装或检查 WebView2。\n是否打开助手日志目录？QQ 绑定和运行数据会保留。\0".encode_utf16().collect();
        let title: Vec<u16> = "Codex QQ 助手\0".encode_utf16().collect();
        if MessageBoxW(0, text.as_ptr(), title.as_ptr(), 0x14) == 6 {
            open_logs();
        }
    }
}

fn show_main(app: &AppHandle) {
    INITIAL_WINDOW.user_opened();
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn shutdown_bridge(app: &AppHandle) {
    save_window(app);
    if let Some(state) = app.try_state::<BridgeState>() {
        if let Ok(mut bridge) = state.0.lock() {
            bridge.shutdown();
        }
    }
}

fn save_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        window_state::save(&window.as_ref().window());
    }
}

fn main() {
    let _ = profile::test_root(); // Invalid isolation arguments must never use real data.
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() == 4
        && (args[1] == "--verify-update" || args[1] == "--check-update-compatibility")
    {
        let result = updates::verify_cli(
            std::path::Path::new(&args[2]),
            std::path::Path::new(&args[3]),
            args[1] == "--check-update-compatibility",
        );
        std::process::exit(if result.is_ok() { 0 } else { 4 });
    }
    #[cfg(not(debug_assertions))]
    if std::env::args().any(|s| s == "--health-check") {
        let ok = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .is_some_and(|root| integrity::verify(&root).is_ok());
        std::process::exit(if ok { 0 } else { 3 });
    }
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(updates::Updates::default())
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            if !args.iter().any(|arg| arg == "--startup") {
                show_main(app);
            }
        }))
        .invoke_handler(tauri::generate_handler![
            app_request,
            app_snapshot,
            app_exit,
            app_reset_window,
            app_appearance,
            updates::app_update
        ])
        .setup(|app| {
            let bridge = ServiceBridge::spawn(app.handle());
            app.manage(BridgeState(Arc::new(Mutex::new(bridge))));

            let open = MenuItem::with_id(app, "open", "打开主窗口", true, None::<&str>)?;
            let check = MenuItem::with_id(app, "check", "重新检查状态", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出助手", true, None::<&str>)?;
            let logs = MenuItem::with_id(app, "logs", "打开日志目录", true, None::<&str>)?;
            let status = MenuItem::with_id(
                app,
                "runtime-status",
                "助手状态：待核对",
                false,
                None::<&str>,
            )?;
            let startup = MenuItem::with_id(
                app,
                "startup-status",
                "Windows 自启：待核对",
                false,
                None::<&str>,
            )?;
            let menu = Menu::with_items(app, &[&status, &open, &check, &logs, &startup, &quit])?;
            app.manage(TrayStatus {
                runtime: status,
                startup,
            });
            let icon = app
                .default_window_icon()
                .cloned()
                .ok_or("应用图标资源缺失")?;
            let app_handle = app.handle().clone();
            let tray = TrayIconBuilder::new()
                .menu(&menu)
                .icon(icon)
                .tooltip("Codex QQ 助手")
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "open" => show_main(app),
                    "check" => {
                        let _ = app.emit("tray-refresh", ());
                    }
                    "quit" => {
                        shutdown_bridge(app);
                        app.exit(0);
                    }
                    "logs" => open_logs(),
                    _ => {}
                })
                .on_tray_icon_event(move |_tray, event| {
                    if matches!(
                        event,
                        TrayIconEvent::DoubleClick {
                            button: MouseButton::Left,
                            ..
                        }
                    ) {
                        show_main(&app_handle);
                    }
                })
                .build(app)?;
            app.manage(tray);

            if let Some(window) = app.get_webview_window("main") {
                let _ = paint_background(&window, tauri::Theme::Light);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::ThemeChanged(theme) = event {
                if let Some(webview) = window.app_handle().get_webview_window("main") {
                    let _ = paint_background(&webview, *theme);
                }
            }
            if matches!(event, WindowEvent::Moved(_) | WindowEvent::Resized(_)) {
                window_state::schedule_save(window);
            }
            if matches!(event, WindowEvent::ScaleFactorChanged { .. }) {
                window_state::schedule_fit(window);
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                window_state::save(window);
                api.prevent_close();
                let _ = window.emit("window-hidden", ());
                let _ = window.hide();
            }
        });

    let application = match builder.build(tauri::generate_context!()) {
        Ok(app) => app,
        Err(_) => {
            native_failure();
            return;
        }
    };
    application.run(|app, event| {
        if matches!(event, RunEvent::Ready) {
            if let Some(window) = app.get_webview_window("main") {
                window_state::initialize(&window.as_ref().window());
            }
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(10)).await;
                if INITIAL_WINDOW.failed(login_launch()) {
                    show_main(&app);
                }
            });
        }
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            shutdown_bridge(app);
        }
    });
}
