// drainhound's Firefox process bridge: which site each content process shows (the first line of an AutoConfig file is skipped).
//
// The battery menu lists a Firefox-family browser's content processes as
// "Firefox web · PID 1234". With this script the browser writes, every 5 s,
// each content process's pid, start time, site and tab titles to
// $XDG_RUNTIME_DIR/firefox-processes/<browser pid>.json (mode 0600), and the
// collector shows "Firefox · example.org · Example · PID 1234" instead. It runs
// in Firefox, LibreWolf, Floorp, Zen, Waterfox and other forks. Private windows
// are never written. Point the collector elsewhere with [apps] firefox_processes.
//
// It is an AutoConfig script, which runs privileged in the browser's main
// process. To install it, in the browser's installation directory (the one with
// the `firefox` binary: /usr/lib/firefox, /usr/lib/librewolf, /opt/zen, ...):
//
//   1. defaults/pref/drainhound-bridge.js, containing:
//        pref("general.config.filename", "drainhound-bridge.cfg");
//        pref("general.config.obscure_value", 0);
//        pref("general.config.sandbox_enabled", false);
//   2. drainhound-bridge.cfg: a copy of this file.
//   3. Restart the browser. about:support's "Important Modified Preferences"
//      lists general.config.filename once it is loaded; errors go to the
//      Browser Console (Ctrl+Shift+J).
//
// On NixOS, programs.firefox.autoConfig (wrapFirefox's extraPrefs) takes this
// file's contents. Flatpak and Snap builds can't load AutoConfig. A package
// update can replace the installation directory: install it again then.
(() => {
  const { classes: Cc, interfaces: Ci, utils: Cu } = Components;
  Cu.importGlobalProperties(["IOUtils", "PathUtils", "TextDecoder"]);
  const runtime = Services.env.get("XDG_RUNTIME_DIR");
  if (!runtime) return;
  const directory = PathUtils.join(runtime, "firefox-processes");
  const path = PathUtils.join(directory, `${Services.appinfo.processID}.json`);
  let busy = false;
  const clean = value => String(value || "").replace(/[\x00-\x1f\x7f]/g, " ").slice(0, 160);
  async function sample() {
    if (busy) return;
    busy = true;
    try {
      const info = await ChromeUtils.requestProcInfo();
      const processes = [];
      for (const child of info.children) {
        // Origin attributes include privateBrowsingId. Do not export private sites.
        if (!child.origin || /(?:\^|&)privateBrowsingId=[1-9]/.test(child.origin)) continue;
        if (!["webIsolated", "web", "withCoopCoep", "webServiceWorker"].includes(child.type)) continue;
        let stat;
        // procfs reports a zero file size, so request a bounded byte read explicitly.
        try {
          stat = new TextDecoder().decode(await IOUtils.read(`/proc/${child.pid}/stat`, { maxBytes: 4096 }));
        } catch { continue; }
        const start = Number(stat.slice(stat.lastIndexOf(")") + 2).split(/\s+/)[19]);
        if (!Number.isSafeInteger(start)) continue;
        const titles = [...new Set((child.windows || [])
          .filter(win => win.isProcessRoot && win.documentTitle)
          .map(win => clean(win.documentTitle)))];
        processes.push({ pid: child.pid, start,
          origin: clean(child.origin.split("^")[0]), titles: titles.slice(0, 3) });
      }
      await IOUtils.makeDirectory(directory, { permissions: 0o700, ignoreExisting: true });
      const temporary = `${path}.tmp`;
      await IOUtils.writeUTF8(temporary, JSON.stringify({ timestamp: Date.now() / 1000,
        browser: clean(Services.appinfo.name), processes }));
      await IOUtils.setPermissions(temporary, 0o600);
      await IOUtils.move(temporary, path);
    } catch (error) {
      // Failure leaves a stale snapshot, which the collector ignores automatically.
      Cu.reportError(`drainhound process bridge: ${error}`);
    } finally { busy = false; }
  }
  const timer = Cc["@mozilla.org/timer;1"].createInstance(Ci.nsITimer);
  const start = () => {
    sample();
    timer.initWithCallback(sample, 5000, Ci.nsITimer.TYPE_REPEATING_SLACK);
  };
  Services.obs.addObserver(function ready() {
    Services.obs.removeObserver(ready, "final-ui-startup");
    start();
  }, "final-ui-startup");
  Services.obs.addObserver(() => { timer.cancel(); }, "quit-application");
})();
