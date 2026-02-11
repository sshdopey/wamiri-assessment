import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import * as pdfjsLib from "pdfjs-dist";
import "pdfjs-dist/build/pdf.worker.min.mjs";

interface PdfViewerProps {
  url: string;
  className?: string;
}

/**
 * Lightweight PDF viewer that renders pages as canvases via pdf.js.
 * No browser chrome, no toolbars — just the document, clean and direct.
 */
export function PdfViewer({ url, className = "" }: PdfViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function render() {
      if (!containerRef.current) return;
      setLoading(true);
      setError(null);

      // Clear previous canvases
      containerRef.current.innerHTML = "";

      try {
        const pdf = await pdfjsLib.getDocument(url).promise;
        if (cancelled) return;

        for (let i = 1; i <= pdf.numPages; i++) {
          const page = await pdf.getPage(i);
          if (cancelled) return;

          // Fit to container width
          const containerWidth = containerRef.current!.clientWidth - 32; // 16px padding each side
          const unscaledViewport = page.getViewport({ scale: 1 });
          const scale = Math.min(containerWidth / unscaledViewport.width, 2.5);
          const viewport = page.getViewport({ scale });

          // Use higher resolution for crisp rendering on retina
          const dpr = window.devicePixelRatio || 1;

          const canvas = document.createElement("canvas");
          canvas.width = Math.floor(viewport.width * dpr);
          canvas.height = Math.floor(viewport.height * dpr);
          canvas.style.width = `${Math.floor(viewport.width)}px`;
          canvas.style.height = `${Math.floor(viewport.height)}px`;
          canvas.style.display = "block";
          canvas.style.margin = "0 auto";

          if (i > 1) {
            // Page separator
            const sep = document.createElement("div");
            sep.style.height = "12px";
            containerRef.current!.appendChild(sep);
          }

          containerRef.current!.appendChild(canvas);

          const ctx = canvas.getContext("2d")!;
          ctx.scale(dpr, dpr);

          await page.render({ canvas, canvasContext: ctx, viewport }).promise;
        }
      } catch (e) {
        if (!cancelled) {
          console.error("PDF render error:", e);
          setError("Failed to render PDF");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    render();
    return () => {
      cancelled = true;
    };
  }, [url]);

  return (
    <div className={`relative w-full h-full overflow-auto ${className}`}>
      {loading && (
        <div className="absolute inset-0 flex items-center justify-center bg-muted/30 z-10">
          <Loader2 className="h-6 w-6 text-muted-foreground animate-spin" />
        </div>
      )}
      {error && (
        <div className="absolute inset-0 flex items-center justify-center">
          <p className="text-sm text-muted-foreground">{error}</p>
        </div>
      )}
      <div ref={containerRef} className="p-4" />
    </div>
  );
}
