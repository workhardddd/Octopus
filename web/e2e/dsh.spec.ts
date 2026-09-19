import { test, expect, type Page } from "@playwright/test";

// DSH (DeepSeek Harness) end-to-end: the credential dialog, and a real turn.
//
// The dialog test is deterministic (the credential endpoints are mocked; the
// backend's own logic is covered by tests/test_dsh_home.py and
// tests/test_harness_dsh.py). The turn test needs the installed `dsh` AND a
// DeepSeek key handed to the runner through `DEEPSEEK_API_KEY`, exactly like
// the codex spec needs a signed-in codex — it auto-skips otherwise rather than
// pretending the path works (dsh-harness.md §6).

const TOKEN = "changeme";
const API = "http://localhost:8765/api";
const OWNED = new Set(["DSH E2E"]);

/** Click the new-session "+" on the default "Octo" agent's row. The button is
 * per-agent, and specs share one in-memory backend DB, so a bare
 * ".btn-session-add" turns ambiguous once a concurrent spec creates another
 * agent. Scoping to Octo keeps it unambiguous. */
const addOctoSession = (page: Page) =>
  page.locator(".agent-item", { hasText: "Octo" }).locator(".btn-session-add").click();

const login = async (page: Page) => {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
};

// A DSH turn pays for whatever DSH has to do on first use (its profile
// workspace, then the model call), and the global 30s default is far too short
// for a real one — the same reason codex.spec.ts raises it.
test.describe.configure({ timeout: 180_000 });

test.afterAll(async ({ request }) => {
  const headers = { Authorization: `Bearer ${TOKEN}` };
  const res = await request.get(`${API}/sessions`, { headers });
  if (res.ok()) {
    for (const s of (await res.json()) as { id: string; name: string }[]) {
      if (OWNED.has(s.name)) {
        await request.delete(`${API}/sessions/${s.id}`, { headers }).catch(() => {});
      }
    }
  }
  const creds = await request.get(`${API}/credentials`, { headers });
  if (creds.ok()) {
    for (const c of (await creds.json()) as { id: string; label: string }[]) {
      if (c.label.startsWith("DSH E2E")) {
        await request.delete(`${API}/credentials/${c.id}`, { headers }).catch(() => {});
      }
    }
  }
});

test("the DSH dialog saves a pasted API key", async ({ page }) => {
  let posted: Record<string, unknown> | null = null;
  let saved = false;
  const credential = {
    id: "dsh-cred-1",
    backend: "dsh",
    label: "DSH E2E key",
    auth_type: "api_key",
    created_at: "2026-09-18T00:00:00Z",
  };

  await page.route("**/api/credentials**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      posted = JSON.parse(request.postData() || "{}");
      saved = true;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(credential),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(saved ? [credential] : []),
    });
  });

  await login(page);
  await page.locator(".btn-manage-harness").click();
  await page.locator(".btn-credential-add").first().click();

  // DSH is the third choice: no sign-in to drive, just a name and a key.
  await page.locator(".btn-choose-dsh").click();
  const dialog = page.locator('[role="dialog"]', { hasText: "Add a DSH API key" });
  await expect(dialog).toBeVisible();
  await dialog.locator("#dsh-label").fill("DSH E2E key");
  await dialog.locator(".dsh-api-key").fill("sk-e2e-not-a-real-key");
  await dialog.locator(".btn-dsh-save").click();

  // The credential lands as a DSH one, named for the harness.
  await expect(dialog).toHaveCount(0);
  expect(posted).toEqual({
    backend: "dsh",
    label: "DSH E2E key",
    auth_type: "api_key",
    secret: "sk-e2e-not-a-real-key",
  });
  const item = page.locator(".credential-item", { hasText: "DSH E2E key" });
  await expect(item).toBeVisible();
  await expect(item.locator(".credential-badge.backend-dsh")).toBeVisible();
  await expect(item).toContainText("DeepSeek (DSH)");
});

test("create a DSH session via the UI and get a real response @llm", async ({
  page,
  request,
}) => {
  const key = process.env.DEEPSEEK_API_KEY;
  test.skip(!key, "DEEPSEEK_API_KEY is not set for the e2e host");

  const headers = {
    Authorization: `Bearer ${TOKEN}`,
    "Content-Type": "application/json",
  };
  const be = await request.get(`${API}/backends`, { headers });
  const available: string[] = be.ok() ? (await be.json()).available : [];
  test.skip(!available.includes("dsh"), "the dsh backend is not available on this host");

  // A DSH credential is what a turn authenticates with, so store one first —
  // the dialog's own path is covered above.
  const created = await request.post(`${API}/credentials`, {
    headers,
    data: {
      backend: "dsh",
      label: "DSH E2E turn",
      auth_type: "api_key",
      secret: key,
    },
  });
  expect(created.ok()).toBeTruthy();

  await login(page);
  await addOctoSession(page);
  await page.locator(".btn-session-advanced").click();
  const engine = page.locator(".session-backend-select");
  await expect(engine).toBeVisible();
  await engine.selectOption("dsh");
  // A DSH session authenticates with its own credential — unlike codex, it has
  // no host login to fall back on (each agent has an isolated DSH home).
  const credential = page.locator(".session-credential-select");
  await expect(credential).toBeVisible();
  await credential.selectOption({ label: "DSH E2E turn" });
  await page
    .locator('.session-create input[placeholder="Session name"]')
    .fill("DSH E2E");
  await page.locator(".session-working-dir").fill("/tmp");
  await page.locator("button.btn-create").click();
  await expect(page.locator(".chat-header .crumb-current")).toHaveText("DSH E2E");

  const sessions = await (
    await request.get(`${API}/sessions`, { headers })
  ).json();
  const sess = sessions.find((s: { name: string }) => s.name === "DSH E2E");
  expect(sess.backend).toBe("dsh");

  const input = page.locator(".chat-input-bar textarea");
  await input.fill("Reply with exactly: PONG-DSH. Do not use any tools.");
  await page.locator("button.btn-send").click();

  await expect(page.locator(".msg-user .msg-content")).toContainText("PONG-DSH");
  await expect(page.locator(".msg-assistant .msg-content")).toBeVisible({
    timeout: 120_000,
  });
  const text = await page
    .locator(".msg-assistant .msg-content")
    .first()
    .textContent();
  expect((text || "").toUpperCase()).toContain("PONG-DSH");
  await expect(page.locator(".result-badge")).toBeVisible({ timeout: 120_000 });
});
