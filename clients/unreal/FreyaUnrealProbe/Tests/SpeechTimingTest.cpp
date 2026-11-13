#include "FreyaSpeechTiming.h"
#include <cassert>

int main()
{
    FFreyaSpeechTiming Timing;
    Timing.Queue(0.0, 0.5);
    Timing.Start(0.0);
    Timing.Queue(5.0, 1.5);
    Timing.ObserveQueue(5.1, true);
    assert(!Timing.Ready(5.1, 1.1));
    assert(!Timing.bContinuous); // Never calibrate from a compressed gap.
    Timing.ObserveQueue(6.5, false);
    assert(!Timing.Ready(7.5, 1.1));
    assert(Timing.Ready(7.7, 1.1));

    Timing.Reset();
    Timing.Queue(0.0, 2.0);
    Timing.Start(0.9);
    Timing.ObserveQueue(1.0, false); // Render thread can consume ahead of ears.
    assert(!Timing.Ready(3.5, 1.1));
    assert(Timing.Ready(4.1, 1.1));
    assert(Timing.bContinuous);

    Timing.Reset();
    Timing.Queue(0.0, 1.0);
    Timing.Start(0.0);
    Timing.ObserveQueue(1.0, false);
    Timing.Queue(1.5, 1.0); // A new queue invalidates the old drain observation.
    assert(!Timing.Ready(10.0, 1.1));
    Timing.ObserveQueue(10.0, false);
    assert(!Timing.Ready(10.5, 1.1));
    assert(Timing.Ready(11.2, 1.1));

    Timing.Reset();
    assert(!Timing.Ready(100.0, 1.1));
    assert(Timing.bContinuous);
    return 0;
}
