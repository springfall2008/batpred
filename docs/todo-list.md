# To-do list

- See [GitHub issues](https://github.com/springfall2008/batpred/issues)

## Future Enhancements
- **Clipping Buffer Advanced Configuration:** 
    - **Time Window:** Add manual configuration overrides (e.g., `clipping_buffer_start_time`, `clipping_buffer_end_time`) to allow users to explicitly set or adjust the active window.
    - **Hardware Specifics:** Improve support for different inverter topologies (Hybrid vs. AC-only) and ensure the clipping logic account for various PV connection types (DC-coupled PV vs separate microinverters).
    - **Grid Export Limits:** Integrate G98/G99 export limitations into the clipping buffer calculation, so space is reserved not just for AC inverter clipping, but also when solar production exceeds a lower DNO-imposed export limit.
