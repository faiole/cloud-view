"use client";

import { useParams } from "next/navigation";
import TenantTabs from "@/components/TenantTabs";

export default function Layout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>();
  return <TenantTabs id={id}>{children}</TenantTabs>;
}
