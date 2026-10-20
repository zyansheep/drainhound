# NixOS module: wattif's user services, its root helper (sudo rule and suspend
# hooks) and /etc/wattif/config.toml.
#
#   services.wattif = {
#     enable = true;
#     helper.users = [ "alice" ];      # who may change root settings in experiments
#     settings.freeze.apps = [ "obsidian" ];
#   };
self: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.wattif;
  toml = pkgs.formats.toml {};
  units = {
    collector = "wattif-collector";
    eta = "wattif-eta";
    panel = "wattif-panel";
    experiment = "wattif-experiment";
    experiments = "wattif-experiments";
    reconcile = "wattif-reconcile";
    throttle = "wattif-throttle";
    freeze = "wattif-freeze";
  };
  # The user units find the setuid sudo first (the system profile's is not
  # setuid), then the user's and the system's packages (niri, swaymsg,
  # powerprofilesctl, iw).
  unitPath = "/run/wrappers/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin";
  helper = "/run/current-system/sw/bin/wattif-helper";
  sudo = "/run/wrappers/bin/sudo";
  rule = {
    users = cfg.helper.users;
    runAs = "root";
    commands = [
      {
        command = helper;
        options = ["NOPASSWD"];
      }
    ];
  };
  wanted = lib.genAttrs (
    [units.collector units.eta units.experiments units.freeze]
    ++ lib.optional cfg.panel.preload units.panel
  ) (_: {
    wantedBy = ["graphical-session.target"];
    environment.PATH = lib.mkForce unitPath;
  });
  started = lib.genAttrs [units.panel units.experiment units.reconcile units.throttle] (_: {
    environment.PATH = lib.mkForce unitPath;
  });
in {
  options.services.wattif = {
    enable = lib.mkEnableOption "wattif, battery use, time left and power experiments for laptops";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "wattif.packages.\${system}.default";
      description = "The wattif package.";
    };
    helper = {
      users = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        example = ["alice"];
        description = ''
          Users who may run wattif-helper as root without a password, which
          experiments need for root-only settings (ASPM, NVMe APST, CPU boost
          and frequency cap, Wi-Fi power saving, amdgpu panel power savings and
          clocks). Without any, experiments vary only what a user can change
          (the power profile, the refresh rate, app throttling).
        '';
      };
      sleepHooks = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Run `wattif-helper sleep-safe` before every suspend and
          `sleep-restore` after resume: the boot-time ASPM policy, NVMe APST
          limit and GPU DPM level hold on the way into suspend, and changes
          wait until resume.
        '';
      };
    };
    panel.preload = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Keep the panel loaded (hidden) so it opens at once; it costs ~100 MB.";
    };
    readableRapl = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Make the RAPL package energy counter (/sys/class/powercap/intel-rapl:*/energy_uj)
        readable by every user, so the collector can measure chip power on
        machines without amdgpu's power sensor (most Intel laptops). Linux made
        it root-only in 5.10 because fine-grained energy readings are a side
        channel (PLATYPUS, CVE-2020-8694); enable it only on a machine whose
        users you trust.
      '';
    };
    settings = lib.mkOption {
      inherit (toml) type;
      default = {};
      example = lib.literalExpression ''
        {
          freeze.apps = [ "obsidian" "steam" ];
          stock.abm = { battery = 3; ac = 0; };
          battery.poweroff = true;
        }
      '';
      description = ''
        /etc/wattif/config.toml (see `wattif config` for every key and its
        default). A user's ~/.config/wattif/config.toml overrides it.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [cfg.package];
    environment.etc."wattif/config.toml".source = toml.generate "wattif-config.toml" (lib.recursiveUpdate {
        helper.command = [sudo "-n" helper];
      }
      cfg.settings);

    systemd.packages = [cfg.package];
    systemd.user.services = wanted // started;
    systemd.user.timers.${units.reconcile}.wantedBy = ["graphical-session.target"];

    security.sudo.extraRules = lib.mkIf (cfg.helper.users != [] && !config.security.sudo-rs.enable) [rule];
    security.sudo-rs.extraRules = lib.mkIf (cfg.helper.users != [] && config.security.sudo-rs.enable) [rule];

    powerManagement = lib.mkIf (cfg.helper.users != [] && cfg.helper.sleepHooks) {
      powerDownCommands = "${cfg.package}/bin/wattif-helper sleep-safe || true";
      resumeCommands = "${cfg.package}/bin/wattif-helper sleep-restore || true";
    };

    services.udev.extraRules = lib.mkIf cfg.readableRapl ''
      SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*", RUN+="${pkgs.coreutils}/bin/chmod 0444 /sys%p/energy_uj"
    '';
  };
}
