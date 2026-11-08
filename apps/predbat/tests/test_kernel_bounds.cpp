// Compile as one translation unit so the private curve helpers are tested directly.
#include "../prediction_kernel.cpp"
#include <cassert>

int main()
{
    double curve[101];
    std::fill(std::begin(curve), std::end(curve), 0.5);
    curve[0] = 0.25;
    curve[100] = 0.75;
    assert(calc_percent_limit(-2.0, 10.0) == 0);
    assert(calc_percent_limit(20.0, 10.0) == 100);
    assert(calc_percent_limit(5.0, 10.0) == 50);
    assert(calc_percent_limit(std::numeric_limits<double>::quiet_NaN(), 10.0) == 0);
    assert(calc_percent_limit(std::numeric_limits<double>::infinity(), 10.0) == 0);
    assert(rate_curve_pct(-20, 1.0, 1.0, 1.0, curve, 0.0) == 0.25);
    assert(rate_curve_pct(200, 1.0, 1.0, 1.0, curve, 0.0) == 0.75);
    assert(rate_curve(-2.0, 1.0, 1.0, 1.0, curve, 10.0, 0.0) == 0.25);
    assert(rate_curve(5.0, 1.0, 1.0, 1.0, curve, 10.0, 0.0) == 0.5);
}
