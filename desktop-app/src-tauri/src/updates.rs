use crate::BridgeState;
use sha2::{Digest, Sha256};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};

#[derive(Default)]
struct Data {
    update: Option<Update>,
    bytes: Option<Vec<u8>>,
    downloading: bool,
}
#[derive(Default)]
pub struct Updates {
    data: Mutex<Data>,
    busy: AtomicBool,
    cancel: tokio::sync::Notify,
}
struct Busy<'a>(&'a AtomicBool);
impl Drop for Busy<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

#[tauri::command]
pub async fn app_update(
    app: AppHandle,
    request: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let id = request["requestId"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .ok_or("request_id_invalid")?
        .to_owned();
    let command = request["command"].as_str().ok_or("command_invalid")?;
    let state = app.state::<Updates>();
    let result = if command == "cancel_update" {
        let data = state.data.lock().map_err(|_| "update_busy")?;
        if data.downloading {
            state.cancel.notify_one();
        }
        Ok(serde_json::json!({"status":if data.downloading {"cancelling"} else {"unchanged"}}))
    } else if state.busy.swap(true, Ordering::AcqRel) {
        Err("update_busy".into())
    } else {
        let _busy = Busy(&state.busy);
        match command {
            "check_update" => check(&app).await,
            "download_update" => download(&app).await,
            "apply_update" => apply(&app, &id).await,
            _ => Err("command_invalid".into()),
        }
    };
    Ok(match result {
        Ok(data) => {
            serde_json::json!({"operationId":id,"requestId":id,"status":"confirmed","preview":false,"data":{"update":data}})
        }
        // These checks have definitively failed before downloading or installing.
        Err(code)
            if command == "check_update"
                && matches!(
                    code.as_str(),
                    "update_configuration_invalid"
                        | "update_feed_unavailable"
                        | "update_source_invalid"
                ) =>
        {
            serde_json::json!({"operationId":id,"requestId":id,"status":"confirmed","preview":false,
                "errorCode":code,"data":{"update":{"status":"failure"}}})
        }
        Err(code) => {
            serde_json::json!({"operationId":id,"requestId":id,"status":"unknown","preview":false,"errorCode":code})
        }
    })
}
pub fn valid_source(u: &Update) -> bool {
    u.download_url.scheme() == "https"
        && u.download_url.host_str() == Some("github.com")
        && u.download_url
            .path()
            .starts_with("/IIMovoMII/Codex-QQ/releases/download/app-v")
}
async fn check(app: &AppHandle) -> Result<serde_json::Value, String> {
    let mut update = app
        .updater_builder()
        .timeout(Duration::from_secs(20))
        .build()
        .map_err(|_| "update_configuration_invalid")?
        .check()
        .await
        .map_err(|_| "update_feed_unavailable")?;
    let state = app.state::<Updates>();
    let mut current = state.data.lock().map_err(|_| "update_busy")?;
    let data = if let Some(ref mut u) = update {
        if !valid_source(u) {
            return Err("update_source_invalid".into());
        }
        u.timeout = Some(Duration::from_secs(120));
        let keep = current
            .update
            .as_ref()
            .is_some_and(|old| old.version == u.version && old.signature == u.signature);
        if !keep {
            current.bytes = None;
        }
        serde_json::json!({"status":if current.bytes.is_some(){"verified"}else{"available"},"version":u.version,"notes":u.body,"currentVersion":u.current_version})
    } else {
        current.bytes = None;
        serde_json::json!({"status":"latest","currentVersion":app.package_info().version.to_string()})
    };
    current.update = update;
    Ok(data)
}
async fn download(app: &AppHandle) -> Result<serde_json::Value, String> {
    const MAX_BYTES: usize = 200 * 1024 * 1024;
    let state = app.state::<Updates>();
    let update = {
        let mut value = state.data.lock().map_err(|_| "update_busy")?;
        let update = value.update.clone().ok_or("update_check_required")?;
        value.downloading = true;
        update
    };
    let mut received = 0usize;
    let mut oversized = false;
    let cancel = state.cancel.notified();
    tokio::pin!(cancel);
    cancel.as_mut().enable();
    let result = tokio::select! {
        result=update.download(|n,total| {
            received=received.saturating_add(n);
            if received>MAX_BYTES || total.is_some_and(|t|t>MAX_BYTES as u64) {
                oversized=true; state.cancel.notify_one();
            }
            let _=app.emit("update-progress",serde_json::json!({"received":received,"total":total}));
        },||{})=>result.map_err(|_|"update_download_or_signature_failed".to_owned()),
        _=&mut cancel=>Err("update_download_cancelled".to_owned()),
    };
    state.data.lock().map_err(|_| "update_busy")?.downloading = false;
    if oversized {
        return Err("update_package_too_large".into());
    }
    let bytes = result?;
    if bytes.len() > MAX_BYTES {
        return Err("update_package_too_large".into());
    }
    let size = bytes.len();
    state.data.lock().map_err(|_| "update_busy")?.bytes = Some(bytes);
    Ok(serde_json::json!({"status":"verified","version":update.version,"bytes":size}))
}
async fn apply(app: &AppHandle, id: &str) -> Result<serde_json::Value, String> {
    let app2 = app.clone();
    let id = id.to_owned();
    tauri::async_runtime::spawn_blocking(move || {
        let state=app2.state::<BridgeState>();
        let mut bridge=state.0.lock().map_err(|_|"service_busy")?;
        let guard=bridge.request(serde_json::json!({"command":"get_update_guard","params":{}}))?;
        if guard["data"]["guard"]["allowed"]!=true {return Err("update_wait_for_idle".into());}
        let state=app2.state::<Updates>();
        let (update,bytes)={
            let mut value=state.data.lock().map_err(|_|"update_busy")?;
            (value.update.clone().ok_or("update_check_required")?,value.bytes.take().ok_or("verified_download_required")?)
        };
        update.verify(&bytes).map_err(|_|"update_signature_failed")?;
        let intent=bridge.request(serde_json::json!({"requestId":id,"command":"begin_update","params":{
            "version":update.version,"sourceVersion":update.current_version,"sha256":format!("{:x}",Sha256::digest(&bytes))}}))?;
        if intent["status"]!="confirmed" {return Err("update_intent_not_confirmed".into());}
        bridge.shutdown();
        // Official updater re-verifies, starts NSIS and exits this process.
        // NSIS owns program rollback; binding/data are outside that scope.
        if update.install(bytes).is_err() {
            *bridge=crate::bridge::ServiceBridge::spawn(&app2);
            let _=bridge.request(serde_json::json!({"command":"fail_update","params":{"operationId":id}}));
            return Err("update_install_not_confirmed".into());
        }
        Ok(serde_json::json!({"status":"installing"}))
    }).await.map_err(|_|"update_task_not_confirmed".to_string())?
}

/// Offline release verification uses the pinned key and official verifier.
/// No window, worker, tray, singleton or user configuration is created.
pub fn verify_cli(
    package: &std::path::Path,
    metadata: &std::path::Path,
    compatibility: bool,
) -> Result<(), String> {
    let mut context = tauri::generate_context!();
    context.config_mut().app.windows.clear();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .build(context)
        .map_err(|_| "verification_host_unavailable")?;
    let raw = std::fs::read(metadata).map_err(|_| "metadata_unavailable")?;
    if raw.len() > 128 * 1024 {
        return Err("metadata_too_large".into());
    }
    let raw = serde_json::from_slice(&raw).map_err(|_| "metadata_invalid")?;
    let mut builder = app.updater_builder();
    if !compatibility {
        builder = builder.version_comparator(|_, _| true);
    }
    let update = builder
        .build()
        .map_err(|_| "verification_configuration_invalid")?
        .restore_update(raw)
        .map_err(|_| "metadata_invalid")?
        .ok_or("version_not_newer")?;
    if !valid_source(&update) {
        return Err("update_source_invalid".into());
    }
    let bytes = std::fs::read(package).map_err(|_| "package_unavailable")?;
    update
        .verify(&bytes)
        .map_err(|_| "package_signature_or_signed_version_invalid".to_string())
}
