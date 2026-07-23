"use client";

import * as React from "react";
import { useApi } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Menu, MenuItem, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { DocBlock } from "@/lib/types";

/**
 * Controlled block editor for an article body (RFC P0.8). Renders each `DocBlock` as an editable
 * card with add / delete / reorder controls. It never owns the block array — the parent
 * (article-editor) holds it and autosaves on change.
 */

type BlockType = DocBlock["type"];

const BLOCK_LABELS: Record<BlockType, string> = {
  paragraph: "Paragraph",
  heading: "Heading",
  list: "List",
  code: "Code",
  callout: "Callout",
  image: "Image",
};

const BLOCK_ORDER: BlockType[] = ["paragraph", "heading", "list", "code", "callout", "image"];

function makeBlock(type: BlockType): DocBlock {
  const id = crypto.randomUUID();
  switch (type) {
    case "heading":
      return { id, type, text: "", level: 2 };
    case "list":
      return { id, type, items: [], ordered: false };
    case "image":
      return { id, type, url: "", alt: "" };
    default:
      return { id, type, text: "" };
  }
}

export function BlockEditor({
  value,
  onChange,
}: {
  value: DocBlock[];
  onChange: (blocks: DocBlock[]) => void;
}) {
  const update = (id: string, patch: Partial<DocBlock>) =>
    onChange(value.map((b) => (b.id === id ? { ...b, ...patch } : b)));

  const remove = (id: string) => onChange(value.filter((b) => b.id !== id));

  const move = (index: number, dir: -1 | 1) => {
    const next = index + dir;
    if (next < 0 || next >= value.length) return;
    const copy = value.slice();
    const [item] = copy.splice(index, 1);
    copy.splice(next, 0, item!);
    onChange(copy);
  };

  const add = (type: BlockType) => onChange([...value, makeBlock(type)]);

  return (
    <div className="flex flex-col gap-3" data-testid="block-editor">
      {value.length === 0 && (
        <p className="rounded-md border border-dashed border-border px-3 py-6 text-center text-xs text-muted-foreground">
          No content yet. Add your first block below.
        </p>
      )}

      {value.map((block, i) => (
        <BlockCard
          key={block.id}
          block={block}
          isFirst={i === 0}
          isLast={i === value.length - 1}
          onChange={(patch) => update(block.id, patch)}
          onRemove={() => remove(block.id)}
          onMoveUp={() => move(i, -1)}
          onMoveDown={() => move(i, 1)}
        />
      ))}

      <div>
        <Menu
          align="start"
          trigger={({ toggle }) => (
            <Button variant="outline" size="sm" onClick={toggle}>
              + Add block
            </Button>
          )}
        >
          {(close) =>
            BLOCK_ORDER.map((type) => (
              <MenuItem
                key={type}
                onClick={() => {
                  add(type);
                  close();
                }}
              >
                {BLOCK_LABELS[type]}
              </MenuItem>
            ))
          }
        </Menu>
      </div>
    </div>
  );
}

function BlockCard({
  block,
  isFirst,
  isLast,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  block: DocBlock;
  isFirst: boolean;
  isLast: boolean;
  onChange: (patch: Partial<DocBlock>) => void;
  onRemove: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
}) {
  return (
    <div className="rounded-md border border-border bg-background p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {BLOCK_LABELS[block.type]}
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label="Move block up"
            disabled={isFirst}
            onClick={onMoveUp}
          >
            ↑
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label="Move block down"
            disabled={isLast}
            onClick={onMoveDown}
          >
            ↓
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-destructive"
            aria-label="Delete block"
            onClick={onRemove}
          >
            ✕
          </Button>
        </div>
      </div>
      <BlockFields block={block} onChange={onChange} />
    </div>
  );
}

function BlockFields({
  block,
  onChange,
}: {
  block: DocBlock;
  onChange: (patch: Partial<DocBlock>) => void;
}) {
  switch (block.type) {
    case "paragraph":
      return (
        <Textarea
          value={block.text ?? ""}
          onChange={(e) => onChange({ text: e.target.value })}
          placeholder="Write a paragraph…"
        />
      );

    case "callout":
      return (
        <Textarea
          value={block.text ?? ""}
          onChange={(e) => onChange({ text: e.target.value })}
          placeholder="Callout text (a highlighted note)…"
        />
      );

    case "code":
      return (
        <Textarea
          value={block.text ?? ""}
          onChange={(e) => onChange({ text: e.target.value })}
          placeholder="Code snippet…"
          spellCheck={false}
          className="font-mono text-xs"
        />
      );

    case "heading":
      return (
        <div className="flex items-center gap-2">
          <Input
            value={block.text ?? ""}
            onChange={(e) => onChange({ text: e.target.value })}
            placeholder="Heading text…"
            className={cn(block.level === 2 ? "text-base font-semibold" : "text-sm font-medium")}
          />
          <div className="flex shrink-0 overflow-hidden rounded-md border border-input">
            {([2, 3] as const).map((lvl) => (
              <button
                key={lvl}
                type="button"
                aria-pressed={block.level === lvl}
                onClick={() => onChange({ level: lvl })}
                className={cn(
                  "px-2.5 py-1 text-xs font-medium transition-colors",
                  (block.level ?? 2) === lvl
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/50",
                )}
              >
                H{lvl}
              </button>
            ))}
          </div>
        </div>
      );

    case "list":
      return (
        <div className="flex flex-col gap-2">
          <Textarea
            value={(block.items ?? []).join("\n")}
            onChange={(e) =>
              onChange({
                items: e.target.value.split("\n").map((s) => s.replace(/\r$/, "")),
              })
            }
            placeholder="One list item per line…"
          />
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={block.ordered ?? false}
              onChange={(e) => onChange({ ordered: e.target.checked })}
            />
            Ordered (numbered) list
          </label>
        </div>
      );

    case "image":
      return <ImageFields block={block} onChange={onChange} />;

    default:
      return null;
  }
}

function ImageFields({
  block,
  onChange,
}: {
  block: DocBlock;
  onChange: (patch: Partial<DocBlock>) => void;
}) {
  const api = useApi();
  const [uploading, setUploading] = React.useState(false);
  const [uploadError, setUploadError] = React.useState<string | null>(null);
  const fileRef = React.useRef<HTMLInputElement>(null);

  const onFile = async (file: File) => {
    setUploading(true);
    setUploadError(null);
    try {
      const { key, upload_url, method } = await api.presignUpload(file.name, file.type);
      const res = await fetch(upload_url, {
        method: method || "PUT",
        body: file,
        headers: { "Content-Type": file.type },
      });
      if (!res.ok) throw new Error(`Upload failed (${res.status})`);
      // NOTE: this signed download URL is time-limited; permanent public asset URLs will land
      // with the CDN work. Until then a fresh signed URL is fetched here after upload.
      const { url } = await api.attachmentDownloadUrl(key);
      onChange({ url });
    } catch (err) {
      // Fall back to manual URL paste — the URL Input below stays usable.
      setUploadError(err instanceof Error ? err.message : "Upload failed. Paste a URL instead.");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Input
          value={block.url ?? ""}
          onChange={(e) => onChange({ url: e.target.value })}
          placeholder="Image URL"
        />
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void onFile(f);
            e.target.value = "";
          }}
        />
        <Button
          variant="outline"
          size="sm"
          className="shrink-0"
          disabled={uploading}
          onClick={() => fileRef.current?.click()}
        >
          {uploading ? <Spinner className="h-3.5 w-3.5" /> : "Upload"}
        </Button>
      </div>
      <Input
        value={block.alt ?? ""}
        onChange={(e) => onChange({ alt: e.target.value })}
        placeholder="Alt text (for accessibility)"
      />
      {uploadError && <p className="text-xs text-destructive">{uploadError}</p>}
      {block.url && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={block.url}
          alt={block.alt ?? ""}
          className="max-h-40 w-auto rounded-md border border-border object-contain"
        />
      )}
    </div>
  );
}
