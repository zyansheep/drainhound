#!/usr/bin/env bash
# Behaviour tests for wattif-helper against a fake /sys, /run and /proc/cmdline,
# with logger, udevadm, iw, bootctl and systemctl stubbed. The flake's checks run
# it; by hand: bash helper/test-helper.sh helper/wattif-helper
# Never touches the real /sys or /run: every root the script uses is rewritten.
# check's conditions are strings it evals, so their $… expand there:
# shellcheck disable=SC2016,SC2034
set -u
script=${1:?usage: test-helper.sh path/to/wattif-helper}
unset SUDO_UID SUDO_USER
# The script's own tools (coreutils, util-linux's flock, sed, grep …) for the harness too.
tools=$(sed -n 's/^PATH=\(.*\):\$PATH$/\1/p' "$script")
[ -n "$tools" ] || { echo "no PATH line in $script"; exit 1; }
PATH=$tools:$PATH
bash -n "$script" || exit 1
pass=0; fail=0
check() { # description, condition (eval'd)
  if eval "$2"; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1"; fi
}
setup() {
  ROOT=$(mktemp -d); export ROOT
  mkdir -p "$ROOT"/{sys,run,stubs}
  S=$ROOT/sys
  mkdir -p "$S"/module/pcie_aspm/parameters "$S"/class/nvme/nvme0/power \
    "$S"/class/drm/card1/device "$S"/class/drm/card1-eDP-1/amdgpu "$S"/bus/pci/drivers/xhci_hcd \
    "$S"/bus/pci/devices/0000:c1:00.3/power "$S"/bus/pci/devices/0000:c1:00.0/power "$S"/bus/pci/devices/0000:c1:00.1 \
    "$S"/devices/system/cpu/cpufreq/policy0 "$S"/devices/system/cpu/cpu0
  echo 'default [performance] powersave powersupersave' > "$S"/module/pcie_aspm/parameters/policy
  echo 0 > "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us
  echo auto > "$S"/class/drm/card1/device/power_dpm_force_performance_level
  echo 3 > "$S"/class/drm/card1-eDP-1/amdgpu/panel_power_savings
  echo auto > "$S"/bus/pci/devices/0000:c1:00.3/power/control
  echo auto > "$S"/bus/pci/devices/0000:c1:00.0/power/control
  echo 0x0c0330 > "$S"/bus/pci/devices/0000:c1:00.3/class
  echo 0x030000 > "$S"/bus/pci/devices/0000:c1:00.1/class
  echo 4972000 > "$S"/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq
  echo 4972000 > "$S"/devices/system/cpu/cpufreq/policy0/scaling_max_freq
  ln -s "$S"/devices/system/cpu/cpufreq/policy0 "$S"/devices/system/cpu/cpu0/cpufreq
  ln -s ../../../bus/pci/drivers/xhci_hcd "$S"/bus/pci/devices/0000:c1:00.3/driver
  : > "$S"/bus/pci/drivers/xhci_hcd/unbind; : > "$S"/bus/pci/drivers/xhci_hcd/bind
  echo 'init=/x pcie_aspm.policy=performance nvme_core.default_ps_max_latency_us=0' > "$ROOT/cmdline"
  echo inactive > "$ROOT/hooks-state"
  for tool in logger udevadm iw; do
    printf '#!/bin/sh\necho "%s $*" >> "$ROOT/calls"\n' "$tool" > "$ROOT/stubs/$tool"; chmod +x "$ROOT/stubs/$tool"
  done
  # systemctl show -P ActiveState <a suspend unit>: whatever hooks-state says.
  printf '#!/bin/sh\necho "systemctl $*" >> "$ROOT/calls"\ncat "$ROOT/hooks-state"\n' > "$ROOT/stubs/systemctl"
  chmod +x "$ROOT/stubs/systemctl"
  sed -e "s|^SYS=/sys$|SYS=$ROOT/sys|; s|^RUN=/run$|RUN=$ROOT/run|; s|^CMDLINE=/proc/cmdline$|CMDLINE=$ROOT/cmdline|" \
      -e "s|^PATH=|PATH=$ROOT/stubs:|; s|^CHANGE_WAIT=10 |CHANGE_WAIT=1 |" "$script" > "$ROOT/wattif-helper"
  chmod +x "$ROOT/wattif-helper"
  for root in "SYS=$ROOT/sys" "RUN=$ROOT/run" "CMDLINE=$ROOT/cmdline" "PATH=$ROOT/stubs:" "CHANGE_WAIT=1 "; do
    grep -q "^$root" "$ROOT/wattif-helper" || { echo "not replaced in the copy: $root"; exit 1; }
  done
}
lab() { "$ROOT/wattif-helper" "$@"; }

setup
# Usage and the removed generic writer.
lab sysfs "$S"/power/state mem 2>/dev/null; check "sysfs is gone (usage)" '[ $? -eq 2 ]'
lab trigger pci 2>/dev/null; check "trigger only power_supply" '[ $? -eq 2 ]'
lab trigger power_supply >/dev/null; check "trigger runs udevadm --settle" 'grep -q "udevadm trigger --action=change --settle -s power_supply" "$ROOT/calls"'
check "the lock is root's alone (0600)" '[ "$(stat -c %a "$ROOT/run/wattif-helper.lock")" = 600 ]'
lab cpucap 2000 >/dev/null; check "cpucap writes scaling_max_freq" '[ "$(cat "$S"/devices/system/cpu/cpufreq/policy0/scaling_max_freq)" = 2000000 ]'
lab cpucap none >/dev/null; check "cpucap none: the CPU maximum" '[ "$(cat "$S"/devices/system/cpu/cpufreq/policy0/scaling_max_freq)" = 4972000 ]'
lab cpucap '2000;x' 2>/dev/null; check "cpucap refuses anything else" '[ $? -eq 2 ]'

# set_aspm fails half-way (the fake policy file never shows brackets): power/control is put back and it's logged.
echo 'default performance powersave [powersupersave]' > "$S"/module/pcie_aspm/parameters/policy
lab aspm performance >/dev/null 2>&1; rc=$?
check "a failed aspm change exits non-zero" '[ $rc -ne 0 ] && [ $rc -ne 75 ]'
check "power/control put back after a failure" '[ "$(cat "$S"/bus/pci/devices/0000:c1:00.3/power/control)" = auto ] && [ "$(cat "$S"/bus/pci/devices/0000:c1:00.0/power/control)" = auto ]'
check "the failure is logged" 'grep -q "logger -t wattif-helper -p user.err -- aspm reads" "$ROOT/calls" && grep -q "aspm failed (exit 1)" "$ROOT/calls"'

# The suspend hooks are not sudo's.
SUDO_UID=1000 lab sleep-safe 2>/dev/null; check "sleep-safe refuses under sudo" '[ $? -eq 2 ] && [ ! -e "$ROOT/run/wattif-helper/sleeping" ]'
SUDO_UID=1000 lab sleep-restore 2>/dev/null; check "sleep-restore refuses under sudo" '[ $? -eq 2 ]'

# sleep-safe: GPU DPM back to auto, APST to the cmdline value (both noted); the flag makes changes refuse.
echo 'default [performance] powersave powersupersave' > "$S"/module/pcie_aspm/parameters/policy
echo low > "$S"/class/drm/card1/device/power_dpm_force_performance_level
echo 100000 > "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us
echo activating > "$ROOT/hooks-state"
lab sleep-safe; check "sleep-safe succeeds" '[ $? -eq 0 ]'
echo active > "$ROOT/hooks-state"
check "sleep-safe sets gpudpm auto" '[ "$(cat "$S"/class/drm/card1/device/power_dpm_force_performance_level)" = auto ]'
check "sleep-safe sets apst to the cmdline" '[ "$(cat "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us)" = 0 ]'
check "sleep-safe notes gpudpm and apst" 'grep -q "^gpudpm $S/class/drm/card1/device/power_dpm_force_performance_level low$" "$ROOT"/run/wattif-helper/pre-sleep && grep -q "^apst .* 100000$" "$ROOT"/run/wattif-helper/pre-sleep'
check "the sleeping flag is set" '[ -e "$ROOT"/run/wattif-helper/sleeping ]'
check "/run/wattif-helper is root's alone (0700)" '[ "$(stat -c %a "$ROOT/run/wattif-helper")" = 700 ]'
lab gpudpm low 2>/dev/null; check "changes refuse while sleeping (75)" '[ $? -eq 75 ]'
lab abm 0 2>/dev/null; check "abm refuses while sleeping" '[ $? -eq 75 ] && [ "$(cat "$S"/class/drm/card1-eDP-1/amdgpu/panel_power_savings)" = 3 ]'
lab xhci-reset 0000:c1:00.3 2>/dev/null; check "xhci-reset refuses while sleeping" '[ $? -eq 75 ]'
echo "0 $(( $(date +%s) - 7200 ))" > "$ROOT"/run/wattif-helper/sleeping
lab gpudpm low 2>/dev/null; check "an old flag still holds while the hooks' unit is up (a long sleep)" '[ $? -eq 75 ]'
echo deactivating > "$ROOT/hooks-state"
lab gpudpm low 2>/dev/null; check "and right after resume, before sleep-restore ran" '[ $? -eq 75 ]'
lab sleep-restore >/dev/null 2>&1; check "sleep-restore succeeds" '[ $? -eq 0 ]'
echo inactive > "$ROOT/hooks-state"
check "after resume gpudpm stays auto" '[ "$(cat "$S"/class/drm/card1/device/power_dpm_force_performance_level)" = auto ]'
check "after resume apst stays at the cmdline value" '[ "$(cat "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us)" = 0 ]'
check "sleep-restore logs what stayed" 'grep -q "after resume, left at the boot-time values: .*gpudpm .*low" "$ROOT/calls"'
check "sleep-restore clears the flag and the note" '[ ! -e "$ROOT"/run/wattif-helper/sleeping ] && [ ! -e "$ROOT"/run/wattif-helper/pre-sleep ]'
lab gpudpm low >/dev/null; check "changes work after resume" '[ $? -eq 0 ] && [ "$(cat "$S"/class/drm/card1/device/power_dpm_force_performance_level)" = low ]'

# A stale flag (the hooks' unit is down, sleep-restore never ran) is cleared and logged.
echo activating > "$ROOT/hooks-state"
lab sleep-safe
echo failed > "$ROOT/hooks-state"
lab gpudpm low >/dev/null 2>&1; check "a stale flag is cleared" '[ $? -eq 0 ] && [ ! -e "$ROOT"/run/wattif-helper/sleeping ]'
check "the stale flag is logged" 'grep -q "clearing a stale sleeping flag" "$ROOT/calls"'
echo inactive > "$ROOT/hooks-state"

# The lock: a change waits CHANGE_WAIT for another, then gives up busy (75).
( flock 8; sleep 3 ) 8< "$ROOT"/run/wattif-helper.lock &
sleep 0.3
lab cpucap 2000 2>/dev/null; check "a held lock makes changes busy (75)" '[ $? -eq 75 ]'
wait

# xhci-reset: validated BDF, an xHCI controller; unbind, bind.
lab xhci-reset '../../x' 2>/dev/null; check "xhci-reset refuses a bad BDF" '[ $? -eq 2 ]'
lab xhci-reset 0000:c1:00.1 2>/dev/null; check "xhci-reset refuses a non-xHCI device" '[ $? -eq 1 ]'
lab xhci-reset 0000:c1:09.0 2>/dev/null; check "xhci-reset refuses a missing device" '[ $? -eq 1 ]'
lab xhci-reset 2>/dev/null; check "xhci-reset needs a BDF" '[ $? -eq 2 ]'
lab xhci-reset 0000:c1:00.3 > "$ROOT/out" 2>/dev/null; check "xhci-reset rebinds it" '[ $? -eq 0 ] && grep -q "0000:c1:00.3: driver=xhci_hcd" "$ROOT/out"'
check "xhci-reset unbinds and binds" '[ "$(cat "$S"/bus/pci/drivers/xhci_hcd/unbind)" = 0000:c1:00.3 ] && [ "$(cat "$S"/bus/pci/drivers/xhci_hcd/bind)" = 0000:c1:00.3 ]'
rm "$S"/bus/pci/devices/0000:c1:00.3/driver; : > "$S"/bus/pci/drivers/xhci_hcd/unbind
lab xhci-reset 0000:c1:00.3 >/dev/null 2>&1; check "an unbound xHCI controller is bound (no unbind)" '[ $? -eq 1 ] && [ ! -s "$S"/bus/pci/drivers/xhci_hcd/unbind ]'

# Boost: per CPU with the global switch on (amd-pstate), or intel_pstate's no_turbo.
mkdir -p "$S"/devices/system/cpu/cpufreq && echo 1 > "$S"/devices/system/cpu/cpufreq/boost && echo 1 > "$S"/devices/system/cpu/cpufreq/policy0/boost
lab boost 0 >/dev/null; check "boost 0 writes the policy, keeps the global switch" '[ "$(cat "$S"/devices/system/cpu/cpufreq/policy0/boost)" = 0 ] && [ "$(cat "$S"/devices/system/cpu/cpufreq/boost)" = 1 ]'
rm "$S"/devices/system/cpu/cpufreq/boost "$S"/devices/system/cpu/cpufreq/policy0/boost
lab boost 1 2>/dev/null; check "boost without a switch fails" '[ $? -eq 1 ]'
mkdir -p "$S"/devices/system/cpu/intel_pstate && echo 0 > "$S"/devices/system/cpu/intel_pstate/no_turbo
lab boost 0 >/dev/null; check "intel_pstate: boost 0 is no_turbo 1" '[ "$(cat "$S"/devices/system/cpu/intel_pstate/no_turbo)" = 1 ]'
lab boost 2 2>/dev/null; check "boost takes 0 or 1" '[ $? -eq 2 ]'

# A panel without ABM, a machine without NVMe: say so, write nothing.
rm -r "$S"/class/drm/card1-eDP-1/amdgpu
lab abm 1 2>/dev/null; check "abm without an amdgpu panel fails" '[ $? -eq 1 ]'
rm -r "$S"/class/nvme
lab apst 0 2>/dev/null; check "apst without NVMe fails" '[ $? -eq 1 ]'

# next-boot: a plain name only.
lab next-boot '../x' 2>/dev/null; check "next-boot refuses a path" '[ $? -eq 2 ]'
lab next-boot 'a b' 2>/dev/null; check "next-boot refuses spaces" '[ $? -eq 2 ]'

rm -rf "$ROOT"
echo "wattif-helper tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
