---
name: login-banner
description: Set or change the login banner (message of the day) on devices through a reviewed playbook.
capabilities: [inventory.devices.list, ansible.playbooks.list, ansible.playbook.run]
---
# Login banner

1. Confirm the devices with `inventory.devices.list`. A banner change on every device needs the person to say so.
2. Find the banner playbook with `ansible.playbooks.list` and check whether it has a rollback.
3. Propose the change with `ansible.playbook.run`, passing the exact banner text the person gave as a variable. Do
   not reword, shorten or "improve" the text: legal and security teams write banners.
4. Tell the person the change reference, the devices, the diff from the plan and how many approvals it needs.
   Nothing has changed yet.
