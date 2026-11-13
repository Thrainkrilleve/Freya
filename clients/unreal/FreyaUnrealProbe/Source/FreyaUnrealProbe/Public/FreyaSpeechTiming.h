#pragma once

// Game-thread timing for one procedural utterance. Queue drain is only a lower
// bound on device playback, so retain the conservative end and settling tail.
struct FFreyaSpeechTiming
{
    double EndAt = 0.0;
    double QueuedSeconds = 0.0;
    double DrainedAt = -1.0;
    bool bStarted = false;
    bool bContinuous = true;

    void Reset() { *this = FFreyaSpeechTiming(); }

    void Start(double Now)
    {
        bStarted = true;
        EndAt = Now + QueuedSeconds;
    }

    void Queue(double Now, double Seconds)
    {
        DrainedAt = -1.0;
        if (!bStarted) { QueuedSeconds += Seconds; return; }
        if (Now > EndAt + 0.01) bContinuous = false;
        EndAt = (Now > EndAt ? Now : EndAt) + Seconds;
    }

    void ObserveQueue(double Now, bool bHasAudio)
    {
        if (bHasAudio) DrainedAt = -1.0;
        else if (DrainedAt < 0.0) DrainedAt = Now;
    }

    bool Ready(double Now, double Tail) const
    {
        return bStarted && DrainedAt >= 0.0
            && Now >= EndAt + Tail && Now >= DrainedAt + Tail;
    }
};
