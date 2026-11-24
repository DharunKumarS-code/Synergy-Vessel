import { AlertTriangle, BrainCircuit } from 'lucide-react'

const PROVIDERS = { gemini: 'Gemini', groq: 'Groq', mock: 'Mock (simulated)' }

/**
 * AI-generated details for one location suggestion: provider, reasoning, ambiguity and low confidence.
 * Information only: every AI suggestion still needs a person's decision in the existing review workflow.
 * Accepts a preview/import `location_intel` object or a review-queue occurrence (same field names).
 */
export default function AiLocationDetails({ info }) {
  if (!info) return null
  const alternatives = info.ai_alternatives ?? []
  const has = info.ai_suggestion || alternatives.length > 0
  if (!has) return null
  const provider = PROVIDERS[info.ai_provider] ?? info.ai_provider
  return (
    <div className="space-y-0.5 text-[10px]" aria-label="AI details">
      <p className="inline-flex items-center gap-1 text-ink-faint">
        <BrainCircuit size={10} aria-hidden="true" /> AI-generated{provider ? ` · ${provider}` : ''} · not a decision
      </p>
      {info.ai_ambiguous && alternatives.length > 0 && (
        <p className="flex items-start gap-1 font-semibold text-warn" aria-label="AI ambiguity">
          <AlertTriangle size={10} className="mt-px shrink-0" aria-hidden="true" />
          Ambiguous — AI found several plausible locations: {alternatives.join(' · ')}
        </p>
      )}
      {info.ai_low_confidence && <p className="font-semibold text-warn" aria-label="Low AI confidence">Low AI confidence</p>}
      {info.ai_reason && <p className="text-ink-muted" aria-label="AI reasoning">AI reasoning: {info.ai_reason}</p>}
    </div>
  )
}
