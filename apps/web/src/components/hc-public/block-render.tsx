import type { DocBlock } from "@/lib/types";

/**
 * Server Component renderer for an article's structured block body.
 * Blocks are structured data (never raw HTML) so we render every field as *text*
 * — no `dangerouslySetInnerHTML`. Unknown block types render nothing and missing
 * fields are tolerated, so a malformed block can never break the page.
 */
export function BlockRender({ blocks }: { blocks: DocBlock[] }) {
  return (
    <div className="hc-prose space-y-4 leading-relaxed text-foreground">
      {blocks.map((block, i) => (
        <Block key={block.id ?? i} block={block} />
      ))}
    </div>
  );
}

function Block({ block }: { block: DocBlock }) {
  switch (block.type) {
    case "paragraph": {
      if (!block.text) return null;
      return <p className="text-foreground/90">{block.text}</p>;
    }

    case "heading": {
      if (!block.text) return null;
      if (block.level === 3) {
        return <h3 className="mt-6 text-lg font-semibold text-foreground">{block.text}</h3>;
      }
      return <h2 className="mt-8 text-xl font-semibold text-foreground">{block.text}</h2>;
    }

    case "list": {
      // Drop blank items (a trailing newline in the editor would otherwise emit an empty bullet).
      const items = (block.items ?? []).filter((it) => it.trim() !== "");
      if (items.length === 0) return null;
      const className = "ml-6 space-y-1 text-foreground/90";
      const children = items.map((item, i) => (
        <li key={i} className="list-outside">
          {item}
        </li>
      ));
      return block.ordered ? (
        <ol className={`list-decimal ${className}`}>{children}</ol>
      ) : (
        <ul className={`list-disc ${className}`}>{children}</ul>
      );
    }

    case "code": {
      if (!block.text) return null;
      return (
        <pre className="overflow-x-auto rounded-md bg-muted p-4 text-sm">
          <code className="font-mono text-foreground/90">{block.text}</code>
        </pre>
      );
    }

    case "callout": {
      if (!block.text) return null;
      return (
        <aside
          className="rounded-md border-l-4 bg-muted/60 p-4 text-foreground/90"
          style={{ borderLeftColor: "var(--hc-primary)" }}
        >
          {block.text}
        </aside>
      );
    }

    case "image": {
      if (!block.url) return null;
      return (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={block.url}
          alt={block.alt ?? ""}
          loading="lazy"
          className="rounded-md border border-border"
        />
      );
    }

    default:
      return null;
  }
}
