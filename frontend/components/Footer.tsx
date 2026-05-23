export function Footer() {
  return (
    <footer className="border-t border-white/[0.04] bg-slate-950">
      <div className="mx-auto max-w-[1400px] px-6 py-8 text-xs text-slate-500">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
          <div className="max-w-xl space-y-2">
            <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-400">
              Disclaimer
            </p>
            
          </div>
          <div className="space-y-2 sm:text-right">
            <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-400">
              Data sources
            </p>
            <p className="text-slate-500">
              NASA FIRMS &middot; ESA Sentinel &middot; ACLED
            </p>
            <p>
              Data:{" "}
              <a
                href="https://creativecommons.org/licenses/by/4.0/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-orange-400/70 underline underline-offset-2 hover:text-orange-300"
              >
                CC-BY 4.0
              </a>{" "}
              &middot; Code:{" "}
              <a
                href="https://opensource.org/licenses/MIT"
                target="_blank"
                rel="noopener noreferrer"
                className="text-orange-400/70 underline underline-offset-2 hover:text-orange-300"
              >
                MIT
              </a>
            </p>
          </div>
        </div>
      </div>
    </footer>
  );
}
