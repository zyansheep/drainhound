{
  description = "drainhound: where a Linux laptop's battery goes, how long it will last, and what would make it last longer";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = ["x86_64-linux" "aarch64-linux"];
    forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    # Python 3.14 or later (compression.zstd).
    python3PackagesOf = pkgs:
      if nixpkgs.lib.versionAtLeast pkgs.python3.version "3.14"
      then pkgs.python3Packages
      else pkgs.python314Packages;
  in {
    packages = forAll (pkgs: rec {
      drainhound = pkgs.callPackage ./nix/package.nix {python3Packages = python3PackagesOf pkgs;};
      default = drainhound;
    });

    nixosModules.default = import ./nix/module.nix self;
    nixosModules.drainhound = self.nixosModules.default;

    overlays.default = final: _prev: {
      drainhound = final.callPackage ./nix/package.nix {python3Packages = python3PackagesOf final;};
    };

    checks = forAll (pkgs: let
      drainhound = self.packages.${pkgs.stdenv.hostPlatform.system}.drainhound;
    in {
      # The Python tests run in the package's checkPhase.
      package = drainhound;
      # The root helper against a fake /sys, /run and /proc/cmdline, and shellcheck.
      helper = pkgs.runCommand "drainhound-helper-check" {nativeBuildInputs = [pkgs.shellcheck];} ''
        shellcheck --severity=style ${./helper/drainhound-helper}
        shellcheck --shell=bash ${./helper/test-helper.sh}
        ${pkgs.bash}/bin/bash ${./helper/test-helper.sh} ${drainhound}/bin/drainhound-helper
        touch $out
      '';
      # ruff, and basedpyright against the errors in .basedpyright/baseline.json: new ones fail.
      lint = let
        python = (python3PackagesOf pkgs).python.withPackages (ps: with ps; [numpy scipy pygobject3 pycairo pygobject-stubs]);
      in
        pkgs.runCommand "drainhound-lint" {nativeBuildInputs = [pkgs.ruff pkgs.basedpyright];} ''
          cp -r ${self} src && chmod -R u+w src && cd src
          ruff check --no-cache src tests tools
          basedpyright --pythonpath ${python}/bin/python
          touch $out
        '';
      # drainhound in a desktop of its own (tests/session.py): niri nested in a headless sway,
      # drawn by Mesa on the CPU, with waybar running its modules and the panel opening.
      # Its screenshots are the output: `nix build .#checks.<system>.session` and look.
      session = let
        python = (python3PackagesOf pkgs).python.withPackages (ps: [ps.numpy ps.scipy]);
        # The fonts, and fontconfig's own rules (a sandbox has no /etc/fonts/conf.d): without
        # them "sans-serif" is whichever font comes first.
        fonts = pkgs.writeText "fonts.conf" ''
          <?xml version="1.0"?>
          <!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
          <fontconfig>
            <include>${pkgs.makeFontsConf {fontDirectories = with pkgs; [dejavu_fonts noto-fonts-color-emoji font-awesome];}}</include>
            <include>${pkgs.fontconfig.out}/etc/fonts/conf.d</include>
          </fontconfig>
        '';
      in
        pkgs.runCommand "drainhound-session" {
          nativeBuildInputs = with pkgs; [sway niri waybar grim foot dbus];
        } ''
          cp -r ${self} src && chmod -R u+w src && cd src
          mkdir -p $out
          export HOME=$TMPDIR XDG_RUNTIME_DIR=$TMPDIR PYTHONPATH=$PWD/src
          export __EGL_VENDOR_LIBRARY_DIRS=${pkgs.mesa}/share/glvnd/egl_vendor.d LIBGL_DRIVERS_PATH=${pkgs.mesa}/lib/dri
          export FONTCONFIG_FILE=${fonts}
          export DRAINHOUND_SESSION=${drainhound}/bin/drainhound DRAINHOUND_SESSION_SHOTS=$out
          ${python.interpreter} -m unittest -v tests.test_session
        '';
      # The NixOS module evaluates, with the helper's sudo rule and suspend hooks.
      module = let
        system = nixpkgs.lib.nixosSystem {
          inherit (pkgs.stdenv.hostPlatform) system;
          modules = [
            self.nixosModules.default
            {
              services.drainhound = {
                enable = true;
                helper.users = ["alice"];
                settings.freeze.apps = ["obsidian"];
              };
              users.users.alice.isNormalUser = true;
              boot.loader.grub.enable = false;
              fileSystems."/" = {
                device = "none";
                fsType = "tmpfs";
              };
              system.stateVersion = "26.05";
            }
          ];
        };
        c = system.config;
      in
        pkgs.writeText "drainhound-module-check" (builtins.toJSON {
          config = c.environment.etc."drainhound/config.toml".source;
          sudo = c.security.sudo.extraRules;
          sleep = c.powerManagement.powerDownCommands;
          wanted = c.systemd.user.services.drainhound-collector.wantedBy;
          panel = c.systemd.user.services.drainhound-panel.wantedBy;
          timer = c.systemd.user.timers.drainhound-reconcile.wantedBy;
        });
    });

    devShells = forAll (pkgs: {
      default = pkgs.mkShell {
        inputsFrom = [self.packages.${pkgs.stdenv.hostPlatform.system}.drainhound];
        packages = [pkgs.ruff pkgs.shellcheck pkgs.basedpyright (python3PackagesOf pkgs).pygobject-stubs];
        shellHook = ''export PYTHONPATH=$PWD/src''${PYTHONPATH:+:$PYTHONPATH}'';
      };
    });

    formatter = forAll (pkgs: pkgs.alejandra);
  };
}
