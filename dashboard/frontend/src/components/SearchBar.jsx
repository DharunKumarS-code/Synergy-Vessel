import { Search, X } from 'lucide-react'
import { useRef } from 'react'

/** Controlled location search input. Full search behaviour arrives in Phase 3. */
export default function SearchBar({ value, onChange, onSubmit, placeholder = 'Search vessel inspection location...' }) {
  const inputRef = useRef(null)
  return (
    <form
      role="search"
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit?.(value.trim())
      }}
      className="group relative"
    >
      <label htmlFor="location-search" className="sr-only">
        Search vessel inspection location
      </label>
      <Search
        size={20}
        className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-ink-faint transition-colors group-focus-within:text-accent"
        aria-hidden="true"
      />
      <input
        ref={inputRef}
        id="location-search"
        type="search"
        autoComplete="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="h-14 w-full rounded-xl border border-surface-600 bg-surface-900/80 pl-12 pr-28 text-[15px] text-ink placeholder:text-ink-faint shadow-inner transition-colors focus:border-accent/50 focus:outline-none focus:ring-4 focus:ring-accent/10 [&::-webkit-search-cancel-button]:hidden"
      />
      <div className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1">
        {value && (
          <button
            type="button"
            onClick={() => {
              onChange('')
              inputRef.current?.focus()
            }}
            className="focus-ring grid h-9 w-9 place-items-center rounded-lg text-ink-faint hover:bg-surface-800 hover:text-ink"
            aria-label="Clear search"
          >
            <X size={16} />
          </button>
        )}
        <button
          type="submit"
          className="focus-ring h-10 rounded-lg bg-accent px-4 text-sm font-semibold text-surface-950 transition-colors hover:bg-accent-soft disabled:cursor-not-allowed disabled:bg-surface-700 disabled:text-ink-faint"
          disabled={!value.trim()}
        >
          Search
        </button>
      </div>
    </form>
  )
}
