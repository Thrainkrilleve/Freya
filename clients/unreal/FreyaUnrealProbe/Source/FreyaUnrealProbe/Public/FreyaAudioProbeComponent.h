#pragma once
#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "TimerManager.h"
#include "FreyaSpeechTiming.h"
#include "FreyaAudioProbeComponent.generated.h"

class FFreyaLink;
class FFreyaProbeSource;
class UAudioComponent;
class USoundWaveProcedural;

/** Local fixture probe only. Does not connect to Freya or open a microphone. */
UCLASS(ClassGroup=(Freya), meta=(BlueprintSpawnableComponent))
class FREYAUNREALPROBE_API UFreyaAudioProbeComponent : public UActorComponent
{
    GENERATED_BODY()
public:
    UFreyaAudioProbeComponent();

    /** Must match the Live Link subject chosen on the MetaHuman's face. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe")
    FName SubjectName = TEXT("FreyaAudioProbe");

    /**
     * 24 kHz mono float32 LE fixture, at most 30 seconds. A relative path is taken
     * from the project folder. -FreyaFixture=<path> on the command line replaces it.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe")
    FString FixturePath = TEXT("Fixtures/freya_test.f32");

    /** Initialize when play begins and start the fixture once, after the delay. No retries. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe")
    bool bAutoStart = true;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe",
        meta=(ClampMin="0.5", ClampMax="30.0"))
    float AutoStartDelaySeconds = 3.0f;

    /** How far ahead the solver looks into the audio. More is better animation, later. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe",
        meta=(ClampMin="80", ClampMax="240"))
    int32 LookaheadMilliseconds = 80;

    /** Name of the owner's face mesh component. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe")
    FName FaceComponentName = TEXT("Face");

    /**
     * Put the face on Epic's Live Link animation blueprint and name this probe's
     * subject in it, instead of relying on the character blueprint to have done so.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe")
    bool bDriveFaceDirectly = true;

    /** Play the same audio aloud. The solver always receives it at full level. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe|Playback")
    bool bPlayAudibly = true;

    /** Applies to what is heard only, never to what the solver analyses. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe|Playback",
        meta=(ClampMin="0.0", ClampMax="1.0"))
    float Volume = 1.0f;

    /**
     * Delay between feeding the solver and starting the sound, to line lips up with
     * voice. Measured on this project with isolated words: the jaw moves about 0.8 to
     * 0.9 s after the solver is given the sound, so the sound waits that long.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe|Playback",
        meta=(ClampMin="0", ClampMax="1500"))
    int32 AudibleDelayMilliseconds = 900;

    /** Keep adjusting the delay from what is measured as she speaks. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Freya Probe|Playback")
    bool bCalibrateDelay = false;

    UFUNCTION(BlueprintCallable, Category="Freya Probe")
    bool InitializeProbe();

    UFUNCTION(BlueprintCallable, Category="Freya Probe")
    bool StartFixture(const FString& Path);

    /** Begin one utterance of 24 kHz mono speech. Loudness is for the ears only. */
    bool BeginSpeech(int32 SampleRate, float Loudness);

    /** Add signed 16-bit samples to the utterance in progress. */
    void PushSpeech(const int16* Samples, int32 Count);

    /** The utterance in progress has been given in full. */
    void EndSpeech();

    /** True from the first sound queued until all of it has been heard. */
    bool IsSpeechAudible() const;

    /** Stop the current speech: no more audio to the solver, and silence. */
    UFUNCTION(BlueprintCallable, Category="Freya Probe")
    void CancelSpeech();

    UFUNCTION(BlueprintCallable, Category="Freya Probe")
    void StopProbe();

    /** Frames the solver has handed back since the probe started. Zero means no output. */
    UFUNCTION(BlueprintPure, Category="Freya Probe")
    int32 GetSolverFrameCount() const;

    /** Largest curve value seen on the subject since the fixture started. */
    UFUNCTION(BlueprintPure, Category="Freya Probe")
    float GetPeakCurveValue() const { return PeakCurve; }

    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
        FActorComponentTickFunction* ThisTickFunction) override;

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type Reason) override;

private:
    void AutoStart();
    void TrackSync();
    void MeasureSync();
    void ConfigureFace();
    void BeginAudiblePlayback();
    void StopAudiblePlayback();
    void Report(bool bFinal);

    TSharedPtr<FFreyaProbeSource> Source;
    FGuid SourceId;
    FTimerHandle AutoStartTimer;
    FTimerHandle PlaybackTimer;
    /** Set when Freya started this window: the connection to her gateway. */
    TSharedPtr<FFreyaLink> Link;
    float SpeechVolume = 1.0f;
    int64 SpeechSamples = 0;
    double SpeechStartedAt = 0.0;
    FFreyaSpeechTiming SpeechTiming;
    bool bVoiceStarted = false;
    bool bSpeechOpen = false;
    bool bSpeechInputEnded = false;
    /** For measuring how far the lips are from the voice. */
    TArray<float> LoudnessBins;
    TArray<double> JawTimes;
    TArray<float> JawValues;
    int32 JawIndex = -1;
    double FirstFedAt = 0.0;
    int32 LastLipDelayMilliseconds = 0;
    float LastLipMatch = 0.0f;
    float PeakCurve = 0.0f;
    int32 CurvesAtPeak = 0;
    double FixtureStartedAt = 0.0;
    double FixtureSeconds = 0.0;
    double LastReportAt = 0.0;
    bool bFixtureActive = false;
    bool bTakeShots = false;
    int32 ShotsTaken = 0;

    UPROPERTY(Transient)
    TObjectPtr<UAudioComponent> Voice;

    UPROPERTY(Transient)
    TObjectPtr<USoundWaveProcedural> Wave;
};
