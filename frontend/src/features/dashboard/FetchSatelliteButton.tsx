import { format, subDays } from "date-fns";
import { Satellite } from "lucide-react";
import { useStartIngest } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { useToasts } from "@/store/toast";
import { useUi } from "@/store/ui";

// A quick-look fetch, not a fake result -- real work, just scoped down:
// - 35 days: a 5-day window (~1 Sentinel-2 revisit) too often finds nothing
//   at all over India, especially in the monsoon; 35 days is wide enough to
//   almost always contain at least one pass without becoming a backfill
//   (still well under the 62-day chunked-backfill threshold).
// - a relaxed cloud threshold: the default 60% "usable" cutoff exists to
//   protect baseline quality over months of history, not a single quick
//   look -- without relaxing it, a real scene found within the window can
//   still be skipped as "too cloudy" and the fetch reports 0 scenes even
//   though one exists.
const QUICK_FETCH_WINDOW_DAYS = 35;
const QUICK_FETCH_MAX_SCENES = 1;
const QUICK_FETCH_MAX_CLOUD_PCT = 95;

/**
 * One-click "fetch satellite data" for a lake that has no processed scenes
 * yet. Wired into `ui.jobId`, the same slot the top bar's pipeline runner
 * polls and reports real progress on, so starting it here surfaces there
 * automatically -- including the invalidation that refreshes observations/
 * indicators once the run actually finishes. There is no faked "instant"
 * result here: the button responds immediately (the job is queued right
 * away), but the tiles only update once real imagery has actually been
 * processed, same as the top bar's own pipeline runner.
 */
export function FetchSatelliteButton({
  waterBodyId,
  lakeName,
  className,
}: {
  waterBodyId: string;
  lakeName: string;
  className?: string;
}) {
  const start = useStartIngest();
  const watchJob = useUi((s) => s.watchJob);
  const push = useToasts((s) => s.push);

  function fetchData() {
    const to = format(new Date(), "yyyy-MM-dd");
    const from = format(subDays(new Date(), QUICK_FETCH_WINDOW_DAYS), "yyyy-MM-dd");
    start.mutate(
      {
        water_body_id: waterBodyId,
        date_from: from,
        date_to: to,
        requested_by: "fetch-satellite-button",
        // Not yet in the generated OpenAPI types (frontend/openapi.json is
        // stale relative to the backend); the backend fields are real.
        max_scenes: QUICK_FETCH_MAX_SCENES,
        max_cloud_pct: QUICK_FETCH_MAX_CLOUD_PCT,
      } as Parameters<typeof start.mutate>[0],
      {
        onSuccess: (j) => {
          watchJob(j.job_id);
          push(`Fetching the latest Sentinel-2 pass for ${lakeName}. Watch progress in the top bar.`, "success");
        },
        onError: (err) => push(`Could not start ingestion for ${lakeName}: ${err.message}`, "error"),
      },
    );
  }

  return (
    <Button
      size="sm"
      variant="secondary"
      disabled={start.isPending}
      onClick={fetchData}
      className={className}
      title={`Fetch the latest available Sentinel-2 pass for ${lakeName} (last ${QUICK_FETCH_WINDOW_DAYS} days)`}
    >
      <Satellite className="h-3.5 w-3.5" />
      {start.isPending ? "Starting…" : "Fetch satellite data"}
    </Button>
  );
}
