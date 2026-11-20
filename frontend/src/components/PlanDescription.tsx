import './PlanDescription.css'

type PlanDescriptionProps = {
  description?: string[]
}

export function PlanDescription({ description }: PlanDescriptionProps) {
  if (!description || description.length === 0) {
    return null
  }

  return (
    <section className="plan-description">
      <div className="plan-description-header">
        <h2>Plan explanation</h2>
      </div>

      <ul className="plan-description-list">
        {description.map((line, index) => (
          <li key={index}>{line}</li>
        ))}
      </ul>
    </section>
  )
}
