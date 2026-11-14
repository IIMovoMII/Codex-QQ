//! Bounded I/O. A timed-out mutation is never replayed on a fresh connection.
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, Instant};
use tauri::AppHandle;
#[cfg(not(debug_assertions))]
use tauri::Manager;

const MAX_FRAME: usize = 1024 * 1024;
static SEQUENCE: AtomicU64 = AtomicU64::new(1);
type WriteJob = (Vec<u8>, Sender<Result<(), String>>);
pub struct ServiceBridge(Result<PythonBridge, String>);
impl ServiceBridge {
    pub fn spawn(app: &AppHandle) -> Self {
        Self(PythonBridge::spawn(app))
    }
    pub fn request(&mut self, value: serde_json::Value) -> Result<serde_json::Value, String> {
        match &mut self.0 {
            Ok(bridge) => bridge.request(value),
            Err(error) => Err(error.clone()),
        }
    }
    pub fn shutdown(&mut self) {
        if let Ok(bridge) = &mut self.0 {
            bridge.shutdown();
        }
    }
}
pub struct PythonBridge {
    child: Child,
    #[cfg(windows)]
    _job: crate::owned_job::OwnedJob,
    input: Sender<WriteJob>,
    output: Receiver<Result<Vec<u8>, String>>,
    healthy: bool,
}

fn read_frame(reader: &mut impl BufRead) -> Result<Vec<u8>, String> {
    let mut line = Vec::new();
    let size = reader
        .take((MAX_FRAME + 1) as u64)
        .read_until(b'\n', &mut line)
        .map_err(|_| "service_read_failed")?;
    if size == 0 || size > MAX_FRAME || !line.ends_with(b"\n") {
        return Err("service_frame_invalid".into());
    }
    Ok(line)
}
impl PythonBridge {
    pub fn spawn(app: &AppHandle) -> Result<Self, String> {
        #[cfg(not(debug_assertions))]
        crate::integrity::verify(
            &app.path()
                .resource_dir()
                .map_err(|_| "应用资源目录不可用")?,
        )?;
        #[cfg(debug_assertions)]
        let _ = app;
        let exe = std::env::current_exe().map_err(|_| "无法核对应用路径")?;
        #[cfg(debug_assertions)]
        let python = std::env::var_os("CODEX_QQ_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("python"));
        #[cfg(not(debug_assertions))]
        let python = app
            .path()
            .resource_dir()
            .map_err(|_| "应用资源目录不可用")?
            .join("python/python.exe");
        let mut command = Command::new(python);
        #[cfg(debug_assertions)]
        {
            let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
            command.env("PYTHONPATH", root.join("src"));
        }
        #[cfg(not(debug_assertions))]
        command.arg("-I");
        let local = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .ok_or("当前用户数据目录不可用")?;
        let home = std::env::var_os("USERPROFILE")
            .map(PathBuf::from)
            .ok_or("当前用户目录不可用")?;
        let data = crate::profile::data().unwrap_or_else(|| local.join("CodexDesktopWeixin"));
        command
            .args([
                "-B",
                "-X",
                "utf8",
                "-u",
                "-m",
                "codex_desktop_weixin.desktop_bridge",
            ])
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .arg("--host-handshake")
            .arg("--startup-executable")
            .arg(&exe)
            .arg("--codex-home")
            .arg(
                crate::profile::test_root()
                    .map(|p| p.join("codex"))
                    .or_else(|| std::env::var_os("CODEX_HOME").map(PathBuf::from))
                    .unwrap_or_else(|| home.join(".codex")),
            )
            .arg("--data-dir")
            .arg(&data)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if std::env::args().any(|arg| arg == "--startup") {
            command.arg("--login-start");
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let mut child = command
            .spawn()
            .map_err(|_| "无法启动内置助手服务，请修复安装")?;
        #[cfg(windows)]
        let job = match crate::owned_job::OwnedJob::attach(&child) {
            Ok(job) => job,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        let mut stdin = child.stdin.take().ok_or("服务输入通道不可用")?;
        let stdout = child.stdout.take().ok_or("服务输出通道不可用")?;
        let stderr = child.stderr.take().ok_or("服务诊断通道不可用")?;
        let (input, writer) = mpsc::channel::<WriteJob>();
        std::thread::spawn(move || {
            while let Ok((payload, reply)) = writer.recv() {
                let result = stdin
                    .write_all(&payload)
                    .and_then(|_| stdin.flush())
                    .map_err(|_| "service_write_failed".to_string());
                let failed = result.is_err();
                let _ = reply.send(result);
                if failed {
                    break;
                }
            }
        });
        let (output_sender, output) = mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let frame = read_frame(&mut reader);
                let failed = frame.is_err();
                if output_sender.send(frame).is_err() || failed {
                    break;
                }
            }
        });
        // Do not log arbitrary stderr, which may contain a library's credential
        // echo. Service-side diagnostics contain only classified safe fields.
        std::thread::spawn(move || {
            let mut stderr = stderr;
            let mut block = [0u8; 4096];
            while stderr.read(&mut block).unwrap_or(0) != 0 {}
        });
        let mut bridge = Self {
            child,
            input,
            output,
            healthy: true,
            #[cfg(windows)]
            _job: job,
        };
        bridge.request(serde_json::json!({"command":"host_ready", "params":{}}))?;
        Ok(bridge)
    }
    pub fn request(&mut self, request: serde_json::Value) -> Result<serde_json::Value, String> {
        self.request_with_timeout(request, Duration::from_secs(65))
    }
    fn request_with_timeout(
        &mut self,
        mut request: serde_json::Value,
        timeout: Duration,
    ) -> Result<serde_json::Value, String> {
        if !self.healthy {
            return Err("服务连接已中断；请重开应用并核对上次操作".into());
        }
        let id = match request.get("requestId") {
            Some(serde_json::Value::String(id)) if !id.is_empty() && id.len() <= 128 => id.clone(),
            None => format!(
                "host-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ),
            _ => return Err("请求编号无效".into()),
        };
        request["version"] = 1.into();
        request["requestId"] = id.clone().into();
        let mut payload = serde_json::to_vec(&request).map_err(|_| "请求格式无效")?;
        payload.push(b'\n');
        if payload.len() > MAX_FRAME {
            return Err("请求过大，未提交".into());
        }
        let (ack, written) = mpsc::channel();
        let result = (|| {
            self.input
                .send((payload, ack))
                .map_err(|_| "service_disconnected")?;
            written
                .recv_timeout(Duration::from_secs(5))
                .map_err(|_| "service_write_timeout")??;
            let line = self
                .output
                .recv_timeout(timeout)
                .map_err(|_| "service_response_timeout")??;
            let response: serde_json::Value =
                serde_json::from_slice(&line).map_err(|_| "service_response_invalid")?;
            if response.get("requestId").and_then(|v| v.as_str()) != Some(&id) {
                return Err("service_response_mismatch".to_string());
            }
            Ok(response)
        })();
        if result.is_err() {
            self.healthy = false;
        }
        result.map_err(|_: String| {
            format!("服务回执未确认（{id}），请重开应用核对结果；不会自动重发")
        })
    }
    pub fn shutdown(&mut self) {
        if self.child.try_wait().ok().flatten().is_some() {
            return;
        }
        if self.healthy {
            let _ = self.request_with_timeout(
                serde_json::json!({"command":"shutdown","params":{}}),
                Duration::from_secs(15),
            );
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if self.child.try_wait().ok().flatten().is_some() {
                return;
            }
            std::thread::sleep(Duration::from_millis(40));
        }
        // Child is the handle returned by our spawn, never a looked-up PID.
        let _ = self.child.kill();
    }
}
impl Drop for PythonBridge {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_truncated_and_oversized_frames() {
        assert!(read_frame(&mut &b"{}"[..]).is_err());
        let oversized = vec![b'x'; MAX_FRAME + 1];
        assert!(read_frame(&mut &oversized[..]).is_err());
        assert_eq!(read_frame(&mut &b"{}\nrest"[..]).unwrap(), b"{}\n");
    }
}
