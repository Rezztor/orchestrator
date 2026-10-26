# User acceptance testing

What we run before calling a release accepted, beyond `pytest`. Nothing here ships in the image.

## Browser check (`browser/`)

The page in a real browser (Playwright's Chromium), with no model and no devices. `stand_in.py` serves the page
against a freshly seeded test database (organisation `acme`: alice the administrator, bob an approver, oscar an
operator, three devices, a recipe, a webhook) and a scripted model. `check.js` then:

- signs in, with the authenticator step;
- asks a read question and proposes a change;
- opens every screen at desktop width;
- adds a device, marks one critical, makes a trigger and saves a policy;
- signs bob in on a phone-sized screen, refuses a rejection with no reason, then approves and runs the change.

It prints `no problems` (exit 0) or the list of problems (exit 1). Screenshots go to `browser/shots/`.

```bash
export ORCH_TEST_DB_URL=postgresql://owner@127.0.0.1:5432/orch_test   # every table is dropped first
python uat/browser/stand_in.py &           # UAT_PORT, default 8099
npm install --no-save playwright@1 && npx playwright install chromium   # once
node uat/browser/check.js
```

The test database is the same one `pytest` uses, so do not run the two at once.
