"use client";

import { CONTEXT_FIELD_CATALOG, type AttributeDataType } from "@/lib/workflows/contract";
import { useAttributeDefinitions } from "@/lib/workflows/workflows-hooks";

/** A selectable field in the predicate editor: a dotted run-context path + its data type. */
export interface FieldOption {
  path: string;
  label: string;
  dataType: AttributeDataType;
}

/**
 * The field catalog for the predicate editor: the static run-context fields (`contract.ts`) plus the
 * workspace's contact custom attributes (`GET /v0/attribute-definitions`) as `contact.custom.<name>`.
 * The picker also allows a free-text path, so this is a convenience, not a hard constraint.
 */
export function useFieldOptions(): { options: FieldOption[]; isLoading: boolean } {
  const contact = useAttributeDefinitions("contact");
  const custom: FieldOption[] = (contact.data ?? []).map((d) => ({
    path: `contact.custom.${d.name}`,
    label: `Contact · ${d.label ?? d.name}`,
    dataType: d.data_type,
  }));
  return {
    options: [
      ...CONTEXT_FIELD_CATALOG.map((f) => ({ path: f.path, label: f.label, dataType: f.data_type })),
      ...custom,
    ],
    isLoading: contact.isLoading,
  };
}

export function dataTypeForPath(path: string, options: FieldOption[]): AttributeDataType {
  return options.find((o) => o.path === path)?.dataType ?? "string";
}
