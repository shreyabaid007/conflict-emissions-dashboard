interface ProvenanceStep {
  source: string;
  description: string;
  timestamp?: string;
}

interface ProvenanceChainProps {
  steps: ProvenanceStep[];
}

export function ProvenanceChain({ steps }: ProvenanceChainProps) {
  return (
    <ol className="border-l border-white/[0.08] pl-4">
      {steps.map((step, i) => (
        <li key={i} className="relative mb-5 last:mb-0">
          <div className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-slate-700 bg-slate-900" />
          <p className="text-sm font-medium text-slate-200">{step.source}</p>
          <p className="mt-0.5 text-xs text-slate-500">{step.description}</p>
          {step.timestamp && (
            <p className="mt-0.5 text-[11px] text-slate-600">{step.timestamp}</p>
          )}
        </li>
      ))}
    </ol>
  );
}
