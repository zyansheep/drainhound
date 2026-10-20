{
  lib,
  python3Packages,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  libadwaita,
  gtk4-layer-shell,
  adwaita-icon-theme,
  makeWrapper,
  makeFontsConf,
  # What drainhound runs: Wi-Fi power save, PipeWire's stream list, notifications,
  # pkill (Waybar refresh) and systemd's tools. The compositor's own CLI (niri
  # msg, swaymsg) comes from the session's PATH.
  iw,
  pipewire,
  libnotify,
  procps,
  systemd,
  util-linux,
  # The root helper's tools.
  bash,
  coreutils,
  gnugrep,
  gnused,
  gawk,
  jq,
}: let
  helperPath = lib.makeBinPath [coreutils util-linux gnugrep gnused gawk systemd iw jq];
in
  python3Packages.buildPythonApplication {
    pname = "drainhound";
    version = builtins.head (builtins.match ".*__version__ = '([^']+)'.*" (builtins.readFile ../src/drainhound/__init__.py));
    pyproject = true;
    src = lib.cleanSourceWith {
      src = ./..;
      filter = path: _type: !(lib.hasSuffix ".nix" path || lib.hasInfix "/result" path);
    };

    build-system = [python3Packages.setuptools];
    dependencies = with python3Packages; [numpy scipy pygobject3 pycairo];
    nativeBuildInputs = [wrapGAppsHook4 gobject-introspection makeWrapper];
    buildInputs = [gtk4 libadwaita gtk4-layer-shell];

    # One wrapper for every command: GTK's typelibs and icons are harmless to the
    # others, and `drainhound panel serve` re-runs itself with gtk4-layer-shell
    # preloaded (DRAINHOUND_LAYER_SHELL_LIB, launch.preload), so nothing else loads GTK.
    dontWrapGApps = true;
    preFixup = ''
      makeWrapperArgs+=(
        "''${gappsWrapperArgs[@]}"
        --prefix XDG_DATA_DIRS : ${adwaita-icon-theme}/share
        --set-default DRAINHOUND_LAYER_SHELL_LIB ${gtk4-layer-shell}/lib/libgtk4-layer-shell.so
        --set DRAINHOUND_SELF $out/bin/drainhound
        --suffix PATH : ${lib.makeBinPath [iw pipewire libnotify procps systemd util-linux]}
      )
    '';

    postInstall = ''
      install -Dm755 helper/drainhound-helper $out/bin/drainhound-helper
      substituteInPlace $out/bin/drainhound-helper \
        --replace-fail '#!/usr/bin/env bash' '#!${lib.getExe bash}' \
        --replace-fail 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH' 'PATH=${helperPath}:$PATH'
      PYTHONPATH=$out/${python3Packages.python.sitePackages}:$PYTHONPATH \
        ${python3Packages.python.interpreter} -m drainhound units --dir $out/lib/systemd/user --exec $out/bin/drainhound
      install -Dm644 -t $out/share/doc/drainhound README.md
      cp -r contrib $out/share/doc/drainhound/
    '';

    nativeCheckInputs = [python3Packages.numpy];
    checkPhase = ''
      runHook preCheck
      # The panel's test draws text: a fontconfig of its own, quiet in the sandbox.
      export FONTCONFIG_FILE=${makeFontsConf {fontDirectories = [];}} XDG_CACHE_HOME=$TMPDIR/cache
      ${python3Packages.python.interpreter} -m unittest discover -s tests -t .
      runHook postCheck
    '';
    pythonImportsCheck = ["drainhound.cli" "drainhound.collector" "drainhound.report"];

    passthru = {inherit helperPath;};

    meta = {
      description = "Where a Linux laptop's battery goes, how long it will last, and what would make it last longer";
      homepage = "https://github.com/zyansheep/drainhound";
      license = lib.licenses.mit;
      platforms = lib.platforms.linux;
      mainProgram = "drainhound";
    };
  }
