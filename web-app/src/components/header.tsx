"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * The bar gains a ground on scroll rather than changing shape.
 *
 * It stays the width of the content it sits over: the table runs edge to
 * edge, and a pill floating in the middle of it read as a separate object
 * dropped on the page rather than as the page's own header. What scroll
 * changes is only the surface -- a rounded, blurred, translucent ground so
 * rows pass under it legibly instead of behind a solid block.
 */
export function Header() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header className="fixed inset-x-0 top-0 z-30 w-full px-2">
      <div
        className={cn(
          "mx-auto mt-2 px-4 transition-all duration-300 sm:px-6",
          scrolled &&
            "rounded-2xl border border-white/10 bg-black/60 shadow-lg shadow-black/40 backdrop-blur-xl",
        )}
      >
        <div className="flex items-center justify-between gap-6 py-3">
          <Link href="/" aria-label="Argus" className="flex items-center gap-2.5">
            <Image
              src="/argus.svg"
              alt=""
              width={22}
              height={22}
              priority
              className="opacity-90"
            />
            <span className="font-[family-name:var(--font-crimson)] text-xl tracking-tight">
              Argus
            </span>
          </Link>

          <nav className="flex items-center gap-6 text-sm">
            <Link
              href="https://github.com/OlympusARC/Argus"
              target="_blank"
              rel="noopener noreferrer"
              className="text-muted-foreground transition-colors duration-150 hover:text-foreground"
            >
              Github
            </Link>
          </nav>
        </div>
      </div>
    </header>
  );
}
