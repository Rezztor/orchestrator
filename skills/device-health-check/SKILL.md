---
name: device-health-check
description: Check which devices at a site answer and what they run, before a change or when someone reports a problem.
capabilities: [inventory.devices.list, ansible.facts.read]
---
# Device health check

1. List the devices in scope with `inventory.devices.list`, filtered by the site, platform or tag the person named.
   If they named none, ask which site before reading facts from the whole estate.
2. Read facts for those devices with `ansible.facts.read`, at most 50 per call.
3. Report, per device: reachable or not, OS or network OS version, model and serial.
4. Call out what stands out: unreachable devices, devices on a different version from their peers, devices the
   inventory lists that Ansible does not know.
5. Do not guess the cause of an unreachable device. Say what was checked and what was not.
