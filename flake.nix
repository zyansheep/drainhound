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
        packages = [pkgs.ruff pkgs.shellcheck];
        shellHook = ''export PYTHONPATH=$PWD/src''${PYTHONPATH:+:$PYTHONPATH}'';
      };
    });

    formatter = forAll (pkgs: pkgs.alejandra);
  };
}
