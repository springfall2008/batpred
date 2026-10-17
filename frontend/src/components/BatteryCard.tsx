type BatteryCardProps = {
  soc: number
  status: string
  power: number
}

function BatteryCard({ soc, status, power }: BatteryCardProps) {
  return (
    <div className="summary-item">
      <div className="summary-item__icon">🔋</div>

      <div className="summary-item__content">
        <span className="summary-item__label">Battery</span>
        <strong className="summary-item__value">{soc}%</strong>
        <span className="summary-item__detail">
          {status} · {power.toFixed(1)} kW
        </span>
      </div>
    </div>
  )
}

export default BatteryCard
