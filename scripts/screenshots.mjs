import { chromium } from "playwright";

const BASE = "http://localhost:3000";
const OUT = "docs/screenshots";

async function main() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });

  // 1. Dashboard full page
  console.log("Taking dashboard screenshot...");
  const dashPage = await ctx.newPage();
  dashPage.on("requestfailed", req => console.log(`  FAILED: ${req.url()} - ${req.failure()?.errorText}`));
  await dashPage.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });
  await dashPage.waitForTimeout(5000);
  await dashPage.screenshot({
    path: `${OUT}/dashboard_v1_final.png`,
    fullPage: true,
  });
  console.log("  -> dashboard_v1_final.png saved");

  // 2. Map view
  console.log("Taking map screenshot...");
  const mapPage = await ctx.newPage();
  await mapPage.goto(`${BASE}/map`, { waitUntil: "networkidle", timeout: 60000 });
  await mapPage.waitForTimeout(5000);
  await mapPage.screenshot({
    path: `${OUT}/map_v1.png`,
    fullPage: true,
  });
  console.log("  -> map_v1.png saved");

  // 3. Event detail
  console.log("Taking event detail screenshot...");
  const eventsResp = await fetch(`${BASE}/api/v1/events?status=PUBLISHED&per_page=200`);
  const eventsData = await eventsResp.json();
  const facResp = await fetch(`${BASE}/api/v1/facilities?per_page=200`);
  const facData = await facResp.json();
  const facilityMap = new Map(facData.data?.map(f => [f.id, f]) ?? []);

  const eventsWithEstimate = eventsData.data?.filter(e => e.estimate != null) ?? [];
  let targetEventId = null;

  for (const evt of eventsWithEstimate) {
    const fac = facilityMap.get(evt.facility_id);
    if (fac && (fac.name.includes("Karoon") || fac.name.includes("Ahvaz"))) {
      targetEventId = evt.id;
      console.log(`  Found Ahvaz/Karoon event: ${evt.id} at ${fac.name}`);
      break;
    }
  }

  if (!targetEventId && eventsWithEstimate.length > 0) {
    eventsWithEstimate.sort((a, b) => (b.estimate?.p50 ?? 0) - (a.estimate?.p50 ?? 0));
    targetEventId = eventsWithEstimate[0].id;
    const fac = facilityMap.get(eventsWithEstimate[0].facility_id);
    console.log(`  Using largest event: ${targetEventId} at ${fac?.name ?? "unknown"}`);
  }

  if (targetEventId) {
    const eventPage = await ctx.newPage();
    await eventPage.goto(`${BASE}/event/${targetEventId}`, {
      waitUntil: "networkidle",
      timeout: 60000,
    });
    await eventPage.waitForTimeout(5000);
    await eventPage.screenshot({
      path: `${OUT}/event_detail_ahvaz.png`,
      fullPage: true,
    });
    console.log("  -> event_detail_ahvaz.png saved");
  } else {
    console.log("  WARNING: No event with estimate found");
  }

  await browser.close();
  console.log("Done!");
}

main().catch(console.error);
