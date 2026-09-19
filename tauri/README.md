# Tauri shell (optional)

The hub runs perfectly well without any native shell — `Steltic.bat` opens a chromeless Edge
window at the same URL, using the same WebView2 engine Tauri would. Build this only when you
want a real installer, an app entry in Add/Remove Programs, and a signed `.exe`.

```
cargo install create-tauri-app
cd tauri && npm create tauri-app@latest    # or wire src-tauri/ into an existing scaffold
cargo tauri build                          # -> src-tauri/target/release/bundle/nsis/*.exe
```

`frontendDist` points at an empty `dist/` on purpose: the window navigates to the hub's own URL
at startup, so the UI is served by Python and updating the UI never means rebuilding Rust.

Output is roughly 6–10 MB. The Python runtime is fetched on first run, not bundled, so the
installer stays small.
