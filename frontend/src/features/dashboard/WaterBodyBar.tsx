import { useEffect, useMemo, useRef, useState } from "react";
import { MapPin, Search } from "lucide-react";
import {
  useAddToWishlist,
  useDiscoverAtPoint,
  useImportDynamic,
  usePlaceSuggestions,
  useWaterBodies,
} from "@/api/hooks";
import type { DiscoveredWaterBodyOut, PlaceSuggestion } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { ErrorState } from "@/components/States";
import { Skeleton } from "@/components/ui/skeleton";
import { PipelineRunner } from "@/features/jobs/PipelineRunner";
import { STATUS_DOT, fmtDateShort, fmtKm2, severityBg, statusLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

const AUTOCOMPLETE_DEBOUNCE_MS = 300;
const AUTOCOMPLETE_MIN_CHARS = 3;
const AUTO_DISCOVER_RADIUS_KM = 15;

/** A discovered-but-not-yet-tracked lake, appended to the strip after a place
 * search (S14). One click imports it, wishlists it and opens it -- "search,
 * then immediately inspect" in a single action. */
function DiscoveredChip({
  candidate,
  district,
}: {
  candidate: DiscoveredWaterBodyOut;
  district: string | null;
}) {
  const importDynamic = useImportDynamic();
  const addToWishlist = useAddToWishlist();
  const select = useUi((s) => s.selectWaterBody);
  const isPlaceholder = candidate.osm_id.startsWith("placeholder:");
  const busy = importDynamic.isPending || addToWishlist.isPending;

  async function inspect() {
    if (isPlaceholder || busy) return;
    if (candidate.already_registered_id) {
      select(candidate.already_registered_id);
      return;
    }
    const imported = await importDynamic.mutateAsync({
      osm_id: candidate.osm_id,
      name: candidate.name,
      kind: candidate.kind,
      geometry: candidate.geometry,
      district: district ?? "Unknown",
      source: "osm",
    });
    addToWishlist.mutate({ water_body_id: imported.water_body_id, custom_name: candidate.name });
    select(imported.water_body_id);
  }

  return (
    <button
      onClick={() => void inspect()}
      disabled={isPlaceholder || busy}
      title={
        isPlaceholder
          ? "OpenStreetMap has no water body mapped here"
          : `${candidate.name} — newly discovered, click to track & inspect`
      }
      className={cn(
        "flex shrink-0 items-center gap-2 rounded-md border border-dashed px-2.5 py-1 text-left transition-colors disabled:cursor-not-allowed",
        isPlaceholder ? "opacity-50" : "border-amber-400 bg-amber-50 hover:bg-amber-100",
      )}
    >
      <MapPin className="h-3.5 w-3.5 shrink-0 text-amber-600" />
      <span className="leading-tight">
        <span className="block max-w-[10rem] truncate whitespace-nowrap text-xs font-medium">
          {candidate.name}
        </span>
        <span className="block whitespace-nowrap text-[10px] text-muted-foreground">
          {busy ? "Adding…" : isPlaceholder ? "Not mapped yet" : `${fmtKm2(candidate.area_km2)} · new`}
        </span>
      </span>
    </button>
  );
}

/** Google-Maps-style "anywhere in India" search: live Nominatim suggestions as
 * the user types, and picking one flies the map there, auto-discovers every
 * water body nearby, and auto-registers + selects the largest real match
 * (already-registered bodies are just selected) so the result is immediately
 * usable without a second click. Fully self-contained: its typed query is
 * NOT the same state as the chip strip's local filter below -- they used to
 * share one variable, so searching "Chilika" also filtered the 50-odd
 * already-tracked Maharashtra bodies down to zero and showed "No water
 * bodies match", even though the search itself was working fine. */
function PlaceSearchBox() {
  const [query, setQuery] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const setSearchResult = useUi((s) => s.setSearchResult);
  const selectWaterBody = useUi((s) => s.selectWaterBody);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(query.trim()), AUTOCOMPLETE_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [query]);

  const suggestQuery = debouncedQ.length >= AUTOCOMPLETE_MIN_CHARS ? debouncedQ : null;
  const suggestions = usePlaceSuggestions(suggestQuery);
  const discoverAtPoint = useDiscoverAtPoint();
  const importDynamic = useImportDynamic();

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  async function pick(s: PlaceSuggestion) {
    setOpen(false);
    setQuery(s.display_name);
    const res = await discoverAtPoint.mutateAsync({
      latitude: s.lat,
      longitude: s.lon,
      radius_km: AUTO_DISCOVER_RADIUS_KM,
    });
    setSearchResult({ centre: res.centre, radiusKm: res.radius_km, district: s.district, items: res.items });
    // Sorted by area descending server-side, so the first real (non-
    // placeholder) hit is the best guess at "the lake the user meant".
    const top = res.items.find((it) => !it.osm_id.startsWith("placeholder:"));
    if (!top) return; // nothing mapped here yet -- the map still flew there, just nothing to select
    if (top.already_registered_id) {
      selectWaterBody(top.already_registered_id);
      return;
    }
    try {
      const imported = await importDynamic.mutateAsync({
        osm_id: top.osm_id,
        name: top.name,
        kind: top.kind,
        geometry: top.geometry,
        district: s.district || "India",
        source: "osm",
      });
      selectWaterBody(imported.water_body_id);
    } catch {
      // Best-effort: the discovered-chip strip below still lets the user
      // import it manually if the automatic import failed.
    }
  }

  return (
    <div className="relative w-56 shrink-0" ref={boxRef}>
      <label className="flex items-center gap-2 rounded-md border bg-card px-2 py-1.5 text-sm">
        <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <input
          className="w-full bg-transparent outline-none placeholder:text-muted-foreground"
          placeholder="Search anywhere in India…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
        />
      </label>
      {open && suggestQuery && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 max-h-64 overflow-y-auto rounded-md border bg-card text-xs shadow-lg">
          {suggestions.isFetching && <div className="px-3 py-2 text-muted-foreground">Searching…</div>}
          {!suggestions.isFetching && (suggestions.data?.items.length ?? 0) === 0 && (
            <div className="px-3 py-2 text-muted-foreground">No places found in India.</div>
          )}
          {suggestions.data?.items.map((s, i) => (
            <button
              key={`${s.lat}-${s.lon}-${i}`}
              onClick={() => void pick(s)}
              className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-accent"
            >
              <MapPin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate">{s.display_name}</span>
            </button>
          ))}
        </div>
      )}
      {/* The dropdown itself closes the instant a suggestion is picked, so
       * this status lives outside it -- discovering + auto-registering the
       * top match is real async work worth showing feedback for. */}
      {(discoverAtPoint.isPending || importDynamic.isPending) && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 rounded-md border bg-card px-3 py-2 text-xs text-muted-foreground shadow-lg">
          {importDynamic.isPending ? "Registering the lake…" : "Discovering water bodies…"}
        </div>
      )}
      {discoverAtPoint.error && !discoverAtPoint.isPending && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 rounded-md border bg-card px-3 py-2 text-xs text-destructive shadow-lg">
          Could not scan that location.
        </div>
      )}
    </div>
  );
}

/**
 * The water-body context bar across the top: search, one chip per body, and
 * the pipeline runner for whichever is selected. Bodies with open alerts sort
 * first, so the queue that matters is reachable without scrolling.
 */
export function WaterBodyBar() {
  const [district, setDistrict] = useState("");
  const { data, isLoading, error, refetch } = useWaterBodies();
  const selected = useUi((s) => s.waterBodyId);
  const select = useUi((s) => s.selectWaterBody);
  const search = useUi((s) => s.search);
  const stripRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLButtonElement>(null);

  // The registry spans several districts; the strip stays usable by narrowing.
  const districts = useMemo(
    () => [...new Set((data?.items ?? []).map((r) => r.district))].sort(),
    [data],
  );

  // Narrowed only by district now -- the search box above is unambiguously
  // "find anywhere in India" and no longer doubles as a free-text filter for
  // this already-tracked strip (that coupling was the actual cause of
  // "No water bodies match" appearing while searching for a place like
  // Chilika Lake that just isn't in the local registry yet).
  const items = useMemo(() => {
    const rows = data?.items ?? [];
    const filtered = rows.filter((r) => !district || r.district === district);
    return [...filtered].sort(
      (a, b) =>
        Number(b.open_alerts > 0) - Number(a.open_alerts > 0) ||
        a.tier - b.tier ||
        a.name.localeCompare(b.name),
    );
  }, [data, district]);

  // The strip scrolls; keep the selected body visible when it changes.
  useEffect(() => {
    const el = activeRef.current;
    const strip = stripRef.current;
    if (!el || !strip) return;
    // Rect maths, not offsetLeft: the strip is not the element's offsetParent.
    const er = el.getBoundingClientRect();
    const sr = strip.getBoundingClientRect();
    const left =
      strip.scrollLeft + (er.left - sr.left) - sr.width / 2 + er.width / 2;
    strip.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
  }, [selected, items.length]);

  // Newly discovered lakes from a place search that aren't already tracked --
  // appended to the strip so the user can click straight into one.
  const discovered = (search?.items ?? []).filter((c) => !c.already_registered_id);

  return (
    <div className="flex min-w-0 items-center gap-3 border-b bg-card px-3 py-2">
      <PlaceSearchBox />

      {districts.length > 1 && (
        <select
          className="shrink-0 rounded-md border bg-card px-1.5 py-1.5 text-xs"
          value={district}
          onChange={(e) => setDistrict(e.target.value)}
          aria-label="District"
        >
          <option value="">All districts</option>
          {districts.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      )}

      <div className="relative min-w-0 flex-1">
        <div ref={stripRef} className="flex items-center gap-2 overflow-x-auto py-0.5">
          {isLoading &&
            [0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-10 w-44 shrink-0 rounded-md" />
            ))}
          {error && <ErrorState error={error} onRetry={() => void refetch()} />}
          {data && items.length === 0 && (
            <span className="text-xs text-muted-foreground">
              No water bodies in this district yet — clear the district filter, or search anywhere in
              India above.
            </span>
          )}
          {items.map((wb) => (
            <button
              key={wb.id}
              ref={selected === wb.id ? activeRef : undefined}
              onClick={() => select(wb.id)}
              title={`${wb.name} · ${wb.district} · ${fmtKm2(wb.area_km2)} · tier ${wb.tier} · ${statusLabel[wb.status]}`}
              className={cn(
                "flex shrink-0 items-center gap-2 rounded-md border px-2.5 py-1 text-left transition-colors",
                selected === wb.id ? "border-primary bg-accent" : "hover:bg-accent/60",
              )}
            >
              <span className={cn("h-2 w-2 shrink-0 rounded-full", STATUS_DOT[wb.status])} />
              <span className="leading-tight">
                <span className="block whitespace-nowrap text-xs font-medium">{wb.name}</span>
                <span className="block whitespace-nowrap text-[10px] text-muted-foreground">
                  {wb.district} · {fmtKm2(wb.area_km2)}
                  {wb.latest_observation && ` · ${fmtDateShort(wb.latest_observation.observed_on)}`}
                </span>
              </span>
              {wb.open_alerts > 0 && wb.max_open_severity && (
                <Badge className={cn("shrink-0", severityBg[wb.max_open_severity])} title={`${wb.open_alerts} open`}>
                  {wb.open_alerts}
                </Badge>
              )}
            </button>
          ))}
          {discovered.map((c) => (
            <DiscoveredChip key={c.osm_id} candidate={c} district={search?.district ?? null} />
          ))}
        </div>
        {/* Hints that the strip keeps going; the registry is longer than the bar. */}
        <div className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-card to-transparent" />
      </div>

      {/* Pinned to the far right corner of the same row -- never wraps to a
       * second line; the strip above shrinks to make room instead. */}
      <div className="ml-auto shrink-0 border-l pl-3">
        <PipelineRunner />
      </div>
    </div>
  );
}
