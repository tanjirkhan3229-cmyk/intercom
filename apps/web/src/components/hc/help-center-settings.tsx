"use client";

import * as React from "react";
import { useHelpCenter, useUpdateHelpCenter } from "@/lib/hc-hooks";
import { Button } from "@/components/ui/button";
import { Input, Spinner } from "@/components/ui/primitives";
import { LoadingState, ErrorState } from "@/components/inbox/states";
import type { HelpCenterInput } from "@/lib/types";

const DEFAULT_COLOR = "#2563eb";

/** `<input type=color>` only accepts 6-digit hex; expand 3-digit and fall back for invalid. */
function toColorInput(hex: string): string {
  const clean = hex.replace("#", "");
  if (/^[0-9a-fA-F]{6}$/.test(clean)) return `#${clean}`;
  if (/^[0-9a-fA-F]{3}$/.test(clean)) {
    return `#${clean
      .split("")
      .map((c) => c + c)
      .join("")}`;
  }
  return DEFAULT_COLOR;
}

/**
 * Help Center configuration form (RFC P0.8): name, primary color, logo URL, default locale.
 * Explicit Save (not autosave) since these are workspace-wide branding settings.
 */
export function HelpCenterSettings() {
  const query = useHelpCenter();
  const update = useUpdateHelpCenter();

  const [name, setName] = React.useState("");
  const [primaryColor, setPrimaryColor] = React.useState(DEFAULT_COLOR);
  const [logoUrl, setLogoUrl] = React.useState("");
  const [locale, setLocale] = React.useState("en");
  const hydrated = React.useRef(false);

  const cfg = query.data;
  React.useEffect(() => {
    if (!cfg || hydrated.current) return;
    hydrated.current = true;
    setName(cfg.name ?? "");
    setPrimaryColor(cfg.primary_color || DEFAULT_COLOR);
    setLogoUrl(cfg.logo_url ?? "");
    setLocale(cfg.default_locale || "en");
  }, [cfg]);

  const onSave = () => {
    const input: HelpCenterInput = {
      name: name || undefined,
      primary_color: primaryColor || null,
      logo_url: logoUrl || null,
      default_locale: locale || "en",
    };
    update.mutate(input);
  };

  if (query.isLoading) return <LoadingState label="Loading settings…" className="h-40" />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} className="h-40" />;
  }

  const validHex = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(primaryColor);

  return (
    <form
      className="flex flex-col gap-4"
      data-testid="help-center-settings"
      onSubmit={(e) => {
        e.preventDefault();
        onSave();
      }}
    >
      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        Help Center
      </p>

      <div className="flex flex-col gap-1">
        <Label>Name</Label>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Help Center" />
      </div>

      <div className="flex flex-col gap-1">
        <Label>Primary color</Label>
        <div className="flex items-center gap-2">
          <input
            type="color"
            value={toColorInput(primaryColor)}
            onChange={(e) => setPrimaryColor(e.target.value)}
            aria-label="Primary color picker"
            className="h-9 w-10 shrink-0 cursor-pointer rounded-md border border-input bg-background"
          />
          <Input
            value={primaryColor}
            onChange={(e) => setPrimaryColor(e.target.value)}
            placeholder="#2563eb"
            className="font-mono"
          />
        </div>
        {!validHex && primaryColor && (
          <p className="text-xs text-destructive">Enter a valid hex color (e.g. #2563eb).</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <Label>Logo URL</Label>
        <Input
          value={logoUrl}
          onChange={(e) => setLogoUrl(e.target.value)}
          placeholder="https://…/logo.png"
        />
      </div>

      <div className="flex flex-col gap-1">
        <Label>Default locale</Label>
        <Input
          value={locale}
          onChange={(e) => setLocale(e.target.value)}
          placeholder="en"
          className="max-w-[8rem]"
        />
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" size="sm" disabled={update.isPending || (!!primaryColor && !validHex)}>
          {update.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Save"}
        </Button>
        {update.isSuccess && <span className="text-xs text-muted-foreground">Saved</span>}
        {update.isError && <span className="text-xs text-destructive">Save failed</span>}
      </div>
    </form>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <label className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </label>
  );
}
