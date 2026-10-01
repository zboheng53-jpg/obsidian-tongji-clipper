/**
 * End-to-End Real Browser Test Suite using Headless Microsoft Edge & Playwright
 * Tests:
 * 1. Visual layout & responsive sizing (search input flex width >= 240px)
 * 2. Real DOM task rendering of all 9 synthetic states over preview adapter
 * 3. Search and status filtering in browser DOM
 * 4. In-memory interactive transitions (pausing cancellation, retry on cancelled, cache cleanup modal)
 * 5. Preview modal accessibility: focus trap, Escape key restoration
 * 6. Timestamp click navigation with strict #clip-t=${sec} hash
 * 7. Settings UI: mutual exclusivity of preset vs custom path, empty path validation guard, keyboard navigation
 */

const assert = require("node:assert/strict");
const path = require("node:path");

const PLAYWRIGHT_PATH = process.env.PLAYWRIGHT_MODULE || "playwright";
const EDGE_PATH = process.env.BROWSER_EXECUTABLE;

(async () => {
  console.log("--- Starting Real Browser (Playwright + Edge) Dashboard Tests ---");

  const pw = require(PLAYWRIGHT_PATH);
  const browser = await pw.chromium.launch({
    ...(EDGE_PATH ? { executablePath: EDGE_PATH } : {}), headless: true,
  });
  try {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
  });

  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];

  // Listen for uncaught page errors
  page.on("pageerror", (err) => {
    errors.push(err.message);
  });

  const previewFilePath = path.resolve(__dirname, "../extension/preview.html");
  const fileUrl = "file:///" + previewFilePath.replace(/\\/g, "/");

  console.log(`Navigating to: ${fileUrl}`);
  await page.goto(fileUrl, { waitUntil: "domcontentloaded" });

  // Wait for initial render of synthetic tasks
  await page.waitForSelector(".task-card", { timeout: 5000 });

  // -------------------------------------------------------------
  // Test 1: Desktop Layout & Search Box Width (>= 240px)
  // -------------------------------------------------------------
  console.log("Test 1: Search Box Desktop Width Verification");
  const searchInput = await page.$("#task-search-input");
  assert.ok(searchInput, "Search input must exist");

  const searchBox = await searchInput.boundingBox();
  assert.ok(searchBox, "Search input bounding box must be measurable");
  console.log(`Measured search input width: ${searchBox.width}px`);
  assert.ok(
    searchBox.width >= 240,
    `Search box width must be at least 240px on desktop (got ${searchBox.width}px)`
  );
  console.log("✓ Search box flex sizing verified (desktop >= 240px)");

  // -------------------------------------------------------------
  // Test 2: Search & Status Filtering
  // -------------------------------------------------------------
  console.log("Test 2: Search & Filter Verification");
  // Type search keyword
  await page.fill("#task-search-input", "高等数学");
  await page.waitForTimeout(100);

  let cards = await page.$$(".task-card");
  assert.equal(cards.length, 1, "Only 1 card matching '高等数学' should be visible");
  let cardTitle = await cards[0].$eval(".task-main-title", (el) => el.textContent);
  assert.match(cardTitle, /高等数学/);

  // Clear search
  await page.fill("#task-search-input", "");
  await page.waitForTimeout(100);

  // Filter by status dropdown: 'completed'
  await page.selectOption("#task-status-filter", "completed");
  await page.waitForTimeout(100);

  cards = await page.$$(".task-card");
  assert.equal(cards.length, 1, "Only completed card should be displayed");
  cardTitle = await cards[0].$eval(".task-main-title", (el) => el.textContent);
  assert.match(cardTitle, /算法设计与分析/);

  // Reset filter to 'all'
  await page.selectOption("#task-status-filter", "all");
  await page.waitForTimeout(100);
  cards = await page.$$(".task-card");
  assert.ok(cards.length >= 9, "All synthetic cards restored");
  console.log("✓ Live search and status filter functioning properly");

  // -------------------------------------------------------------
  // Test 3: Intermediate States and Action Buttons
  // -------------------------------------------------------------
  console.log("Test 3: Intermediate States & Actions Verification");
  // A. Pausing task can be cancelled
  const cancelPausingBtn = await page.$(
    '.task-card:has-text("大学物理") button[data-action="cancel"]'
  );
  assert.ok(cancelPausingBtn, "Pausing task must have cancel button");
  await cancelPausingBtn.click();
  await page.waitForTimeout(200);

  // Verify task transitioned to cancelled
  const physCardStatus = await page.$eval(
    '.task-card:has-text("大学物理") .task-status-pill',
    (el) => el.textContent
  );
  assert.match(physCardStatus, /已取消/);

  // B. Cancelled task has retry button
  const retryBtn = await page.$(
    '.task-card:has-text("工程力学") button[data-action="retry"]'
  );
  assert.ok(retryBtn, "Cancelled task must have retry button");
  await retryBtn.click();
  await page.waitForTimeout(200);

  const mechCardStatus = await page.$eval(
    '.task-card:has-text("工程力学") .task-status-pill',
    (el) => el.textContent
  );
  assert.match(mechCardStatus, /准备任务/);

  // C. Cache cleanup modal
  const cleanupBtn = await page.$(
    '.task-card:has-text("算法设计与分析") button[data-action="cleanup"]'
  );
  assert.ok(cleanupBtn, "Completed task with cache_bytes must have cleanup button");
  await cleanupBtn.click();
  await page.waitForSelector("#cleanup-modal", { state: "visible" });

  // Click confirm cleanup inside modal
  await page.click("#btn-confirm-cleanup");
  await page.waitForSelector("#cleanup-modal", { state: "hidden" });
  console.log("✓ In-memory task actions (cancel pausing, retry cancelled, cleanup cache) verified");

  // -------------------------------------------------------------
  // Test 4: Transcript Preview Modal & Timestamp Link Navigation
  // -------------------------------------------------------------
  console.log("Test 4: Preview Modal & URL Jump with #clip-t=");
  const previewBtn = await page.$(
    '.task-card:has-text("算法设计与分析") button[data-action="preview"]'
  );
  assert.ok(previewBtn, "Completed task must have preview button");
  await previewBtn.click();

  await page.waitForSelector("#preview-modal", { state: "visible" });
  await page.waitForSelector(".segment-item");

  // Verify review guidance tag without fake accuracy metrics
  const reviewTagText = await page.$eval(".segment-review-tag", (el) => el.textContent);
  assert.match(reviewTagText, /建议复核/);
  const modalText = await page.$eval("#preview-modal", (el) => el.textContent);
  assert.doesNotMatch(modalText, /准确率/);
  assert.doesNotMatch(modalText, /%/);

  // Click timestamp button
  await page.click(".segment-time-btn");
  await page.waitForTimeout(100);

  const openedTabs = await page.evaluate(() =>
    window.__preview_test_state__.getOpenedTabs()
  );
  assert.ok(openedTabs.length > 0, "Tab should be opened upon timestamp click");
  const lastTabUrl = openedTabs[openedTabs.length - 1];
  assert.match(lastTabUrl, /#clip-t=\d+/, "URL hash must strictly format as #clip-t=${sec}");

  // Verify tab wrapping as well as Escape restoration.
  await page.focus("#btn-preview-done");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "btn-close-preview");
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "btn-preview-done");
  // Test modal focus trap and escape key restoration
  await page.keyboard.press("Escape");
  await page.waitForSelector("#preview-modal", { state: "hidden" });

  // Verify focus returned to preview button
  const focusedAction = await page.evaluate(() =>
    document.activeElement ? document.activeElement.getAttribute("data-action") : null
  );
  assert.equal(focusedAction, "preview", "Focus must return to preview button after Escape");
  console.log("✓ Preview modal, #clip-t= timestamp link, and focus restoration verified");

  // -------------------------------------------------------------
  // Test 5: Settings Tab & Mutual Exclusivity
  // -------------------------------------------------------------
  console.log("Test 5: Settings Tab & Mutual Exclusivity Verification");
  // Switch to settings tab using keyboard ArrowRight
  await page.focus("#tab-btn-tasks");
  await page.keyboard.press("ArrowRight");
  await page.waitForSelector("#pane-settings", { state: "visible" });

  // A. Check default effective model is preset 'small'
  let effModel = await page.$eval("#lbl-effective-model", (el) => el.textContent);
  assert.equal(effModel, "small");

  // B. Switch to custom model option
  await page.click("#radio-model-custom");
  const isInputCustomEnabled = await page.$eval(
    "#input-model-custom",
    (el) => !el.disabled
  );
  assert.equal(isInputCustomEnabled, true);
  const isSelectPresetDisabled = await page.$eval(
    "#select-model-preset",
    (el) => el.disabled
  );
  assert.equal(isSelectPresetDisabled, true);

  // C. Empty custom path blocks validation and warns
  await page.click("#btn-validate-config");
  await page.waitForSelector("#validation-box", { state: "visible" });
  const valText = await page.$eval("#validation-checks-list", (el) => el.textContent);
  assert.match(valText, /已选择本地自定义模型，但路径为空/);

  // D. Fill valid custom path
  await page.fill("#input-model-custom", "D:\\local-models\\whisper-base-zh");
  effModel = await page.$eval("#lbl-effective-model", (el) => el.textContent);
  assert.equal(effModel, "D:\\local-models\\whisper-base-zh");

  // Validate with valid path
  await page.click("#btn-validate-config");
  await page.waitForTimeout(100);
  const checkResults = await page.$eval("#validation-checks-list", (el) => el.textContent);
  assert.match(checkResults, /架构验证通过/);

  // E. Switch back to preset -> does not covertly use custom path
  await page.click("#radio-model-preset");
  effModel = await page.$eval("#lbl-effective-model", (el) => el.textContent);
  assert.equal(effModel, "small");
  assert.notEqual(effModel, "D:\\local-models\\whisper-base-zh");

  console.log("✓ Settings mutual exclusivity, empty path guard, and validation verified");

  await page.setViewportSize({width: 480, height: 900});
  await page.click("#tab-btn-tasks");
  const layout = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    viewport: innerWidth,
    toolbarHeight: document.querySelector(".toolbar").getBoundingClientRect().height,
  }));
  assert.ok(layout.width <= layout.viewport, "Narrow window must not overflow horizontally");
  assert.ok(layout.toolbarHeight < 200, "Narrow toolbar must not leave a large empty column");
  assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
  console.log("All Playwright real browser tests passed successfully!");
})().catch((err) => {
  console.error("Playwright test failed:", err);
  process.exitCode = 1;
});
