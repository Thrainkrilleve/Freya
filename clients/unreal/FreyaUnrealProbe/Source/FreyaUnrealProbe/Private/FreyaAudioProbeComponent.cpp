#include "FreyaAudioProbeComponent.h"
#include "FreyaLink.h"
#include "HAL/PlatformMisc.h"
#include <atomic>
#include "MetaHumanAudioBaseLiveLinkSubject.h"
#include "MetaHumanAudioBaseLiveLinkSubjectSettings.h"
#include "MetaHumanLocalLiveLinkSource.h"
#include "ILiveLinkClient.h"
#include "LiveLinkTypes.h"
#include "Roles/LiveLinkBasicRole.h"
#include "Components/AudioComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Animation/AnimInstance.h"
#include "GameFramework/Actor.h"
#include "UObject/UnrealType.h"
#include "Sound/SoundWaveProcedural.h"
#include "Engine/World.h"
#include "Engine/Engine.h"
#include "GameFramework/GameUserSettings.h"
#include "GenericPlatform/GenericApplication.h"
#include "HAL/PlatformApplicationMisc.h"
#include "Features/IModularFeatures.h"
#include "Misc/CommandLine.h"
#include "UnrealClient.h"
#include "Kismet/KismetSystemLibrary.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "HAL/FileManager.h"
#include "HAL/PlatformProcess.h"
#include "Misc/ScopeLock.h"
#include "Serialization/Archive.h"

DEFINE_LOG_CATEGORY_STATIC(LogFreyaProbe, Log, All);

namespace
{
    constexpr int32 FixtureSampleRate = 24000;
    constexpr int32 MaxFixtureSeconds = 30;
    // How long after the last audio the solver is given to finish, before the final report.
    constexpr double TailSeconds = 1.0;
    // How long after the sound ends the jaw is still watched before lips and voice
    // are compared: the longest lag looked for, and a little more.
    constexpr double SyncTailSeconds = 1.1;
}

// Audio waiting to be fed to the solver, a little at a time at the pace it would
// be heard. The sampler thread owns the cursor; everything else only adds or clears.
class FFreyaProbeSubject final : public FMetaHumanAudioBaseLiveLinkSubject
{
public:
    FFreyaProbeSubject(ILiveLinkClient* Client, const FGuid& Id, FName Name,
                      UMetaHumanAudioBaseLiveLinkSubjectSettings* Settings)
        : FMetaHumanAudioBaseLiveLinkSubject(Client, Id, Name, Settings) {}

    virtual ~FFreyaProbeSubject() override { Stop(); }

    virtual void Start() override
    {
        SamplerStopping.store(false);
        FMetaHumanAudioBaseLiveLinkSubject::Start();
    }

    virtual void Stop() override
    {
        SamplerStopping.store(true);
        FMetaHumanAudioBaseLiveLinkSubject::Stop();
    }

    void Push(const float* Data, int32 Count)
    {
        FScopeLock Guard(&Mutex);
        // Forget what has been fed already, so a long conversation does not pile up.
        if (Cursor > 0 && (Cursor == Fixture.Num() || Cursor > 10 * FixtureSampleRate))
        {
            Fixture.RemoveAt(0, Cursor, EAllowShrinking::No);
            Cursor = 0;
        }
        Fixture.Append(Data, Count);
    }

    // Drop whatever has not been fed yet. What the solver already has plays out.
    void Cancel()
    {
        FScopeLock Guard(&Mutex);
        Fixture.Reset();
        Cursor = 0;
    }

    int32 SolverFrames() const { return Frames.load(); }

protected:
    virtual void MediaSamplerMain() override
    {
        // The base starts the pipeline asynchronously at lower priority.
        // Its initial completed state must not make the sampler exit before
        // the processing thread has begun. Shutdown also ends this wait.
        const double StartupDeadline = FPlatformTime::Seconds() + 5.0;
        while (!IsRunning())
        {
            if (SamplerStopping.load()) return;
            if (FPlatformTime::Seconds() >= StartupDeadline)
            {
                UE_LOG(LogFreyaProbe, Error, TEXT("Audio sampler: pipeline did not start within five seconds."));
                return;
            }
            FPlatformProcess::SleepNoStats(0.002f);
        }
        UE_LOG(LogFreyaProbe, Display, TEXT("Audio sampler: pipeline running; feeding enabled."));
        double Deadline = FPlatformTime::Seconds();
        while (!SamplerStopping.load() && IsRunning())
        {
            FAudioSample Packet;
            {
                FScopeLock Guard(&Mutex);
                const int32 Count = FMath::Min(480, Fixture.Num() - Cursor);
                if (Count > 0)
                {
                    Packet.Data.Append(Fixture.GetData() + Cursor, Count);
                    Cursor += Count;
                    Packet.NumSamples = Count;
                }
            }
            if (Packet.NumSamples <= 0)
            {
                Deadline = FPlatformTime::Seconds();
                FPlatformProcess::SleepNoStats(0.002f);
                continue;
            }
            Packet.NumChannels = 1;
            Packet.SampleRate = FixtureSampleRate;
            GetSampleTime(FFrameRate(60, 1), Packet.Time, Packet.TimeSource);
            const double Duration = Packet.NumSamples / double(FixtureSampleRate);
            AddAudioSample(MoveTemp(Packet));
            Deadline = FMath::Max(Deadline, FPlatformTime::Seconds()) + Duration;
            FPlatformProcess::SleepNoStats(float(FMath::Max(0.0, Deadline - FPlatformTime::Seconds())));
        }
    }

    // Called with each result the solver produces. Counting them is the plainest
    // evidence that the solver ran at all.
    virtual void ExtractPipelineData(TSharedPtr<UE::MetaHuman::Pipeline::FPipelineData> Data) override
    {
        ++Frames;
        FMetaHumanAudioBaseLiveLinkSubject::ExtractPipelineData(Data);
    }

private:
    FCriticalSection Mutex;
    TArray<float> Fixture;
    int32 Cursor = 0;
    std::atomic<int32> Frames{0};
    std::atomic<bool> SamplerStopping{false};
};

class FFreyaProbeSource final : public FMetaHumanLocalLiveLinkSource
{
public:
    virtual FText GetSourceType() const override { return FText::FromString(TEXT("Freya fixture probe")); }
    bool Push(const float* Data, int32 Count)
    {
        if (!Subject.IsValid()) return false;
        Subject->Push(Data, Count);
        return true;
    }
    void Cancel() { if (Subject.IsValid()) Subject->Cancel(); }
    int32 SolverFrames() const { return Subject.IsValid() ? Subject->SolverFrames() : 0; }
protected:
    virtual TSharedPtr<FMetaHumanLocalLiveLinkSubject> CreateSubject(
        const FName& Name, UMetaHumanLocalLiveLinkSubjectSettings* InSubjectSettings) override
    {
        Subject = MakeShared<FFreyaProbeSubject>(LiveLinkClient, SourceGuid, Name,
            CastChecked<UMetaHumanAudioBaseLiveLinkSubjectSettings>(InSubjectSettings));
        return Subject;
    }
private:
    TSharedPtr<FFreyaProbeSubject> Subject;
};

UFreyaAudioProbeComponent::UFreyaAudioProbeComponent()
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.bStartWithTickEnabled = true;
}

void UFreyaAudioProbeComponent::BeginPlay()
{
    Super::BeginPlay();
    // An ordinary resizable window, never the whole screen, unless -fullscreen is given.
    // Settings saved by an earlier run are overridden, so this holds every time.
    if (!GIsEditor && !FParse::Param(FCommandLine::Get(), TEXT("fullscreen")))
        if (UGameUserSettings* Settings = GEngine ? GEngine->GetGameUserSettings() : nullptr)
        {
            // Sized from the display, not fixed: about three fifths of its usable
            // height, in a portrait shape, in real pixels (the project draws at the
            // display's own resolution, so nothing is stretched).
            FDisplayMetrics Display;
            FDisplayMetrics::RebuildDisplayMetrics(Display);
            const FPlatformRect& Area = Display.PrimaryDisplayWorkAreaRect;
            const int32 Height = FMath::Clamp(FMath::RoundToInt((Area.Bottom - Area.Top) * 0.6f), 480, 2000);
            FIntPoint Size(FMath::RoundToInt(Height * 0.8f), Height);
            FParse::Value(FCommandLine::Get(), TEXT("ResX="), Size.X);
            FParse::Value(FCommandLine::Get(), TEXT("ResY="), Size.Y);
            Settings->SetFullscreenMode(EWindowMode::Windowed);
            Settings->SetScreenResolution(Size);
            Settings->SetFrameRateLimit(60.0f);
            Settings->ApplySettings(false);
        }
    // -FreyaDelay=<milliseconds> tries another sound delay without rebuilding.
    FParse::Value(FCommandLine::Get(), TEXT("FreyaDelay="), AudibleDelayMilliseconds);
    // Started by Freya: she gives the port of her gateway and a key that works once.
    const FString GatewayPort = FPlatformMisc::GetEnvironmentVariable(TEXT("FREYA_GATEWAY_PORT"));
    const FString PairingKey = FPlatformMisc::GetEnvironmentVariable(TEXT("FREYA_PAIRING_KEY"));
    if (!GatewayPort.IsEmpty() && !PairingKey.IsEmpty() && FCString::Atoi(*GatewayPort) > 0)
    {
        const bool bLinked = InitializeProbe();
        UE_LOG(LogFreyaProbe, Display, TEXT("Started by Freya; probe %s. Connecting to her gateway."),
            bLinked ? TEXT("initialized") : TEXT("FAILED to initialize"));
        Link = MakeShared<FFreyaLink>(this, FCString::Atoi(*GatewayPort), PairingKey);
        Link->Start();
        return;
    }
    if (!bAutoStart) return;
    const bool bReady = InitializeProbe();
    UE_LOG(LogFreyaProbe, Display, TEXT("Auto start: probe %s. Fixture begins in %.1f s."),
        bReady ? TEXT("initialized") : TEXT("FAILED to initialize"), AutoStartDelaySeconds);
    if (bReady)
        GetWorld()->GetTimerManager().SetTimer(AutoStartTimer, this,
            &UFreyaAudioProbeComponent::AutoStart, FMath::Clamp(AutoStartDelaySeconds, 0.5f, 30.0f), false);
}

void UFreyaAudioProbeComponent::AutoStart()
{
    FString Path = FixturePath;
    FParse::Value(FCommandLine::Get(), TEXT("FreyaFixture="), Path);
    // One attempt only. A failure is reported, not retried.
    if (!StartFixture(Path))
        UE_LOG(LogFreyaProbe, Error, TEXT("RESULT: the fixture could not be started: %s"), *Path);
}

bool UFreyaAudioProbeComponent::InitializeProbe()
{
    if (Source.IsValid()) return true;
    if (SubjectName.IsNone() || !IModularFeatures::Get().IsModularFeatureAvailable(ILiveLinkClient::ModularFeatureName)) return false;
    ILiveLinkClient& Client = IModularFeatures::Get().GetModularFeature<ILiveLinkClient>(ILiveLinkClient::ModularFeatureName);
    Source = MakeShared<FFreyaProbeSource>();
    SourceId = Client.AddSource(Source);
    auto* Settings = NewObject<UMetaHumanAudioBaseLiveLinkSubjectSettings>(GetTransientPackage());
    Settings->Lookahead = FMath::Clamp(LookaheadMilliseconds, 80, 240);
    Settings->Setup();
    const auto Key = Source->RequestSubjectCreation(SubjectName.ToString(), Settings);
    if (!SourceId.IsValid() || !Key.Source.IsValid()) { StopProbe(); return false; }
    UE_LOG(LogFreyaProbe, Display, TEXT("Source registered. This does not prove solver readiness or output."));
    return true;
}

// The assembled MetaHuman's face reads Live Link through Epic's ABP_MH_LiveLink
// animation blueprint, which names its subject in a variable. Report what the face
// is using and, when asked to, point it at this probe's subject.
void UFreyaAudioProbeComponent::ConfigureFace()
{
    AActor* Owner = GetOwner();
    if (!Owner) return;
    TArray<USkeletalMeshComponent*> Meshes;
    Owner->GetComponents(Meshes);
    for (USkeletalMeshComponent* Mesh : Meshes)
    {
        if (Mesh->GetFName() != FaceComponentName) continue;
        UAnimInstance* Anim = Mesh->GetAnimInstance();
        UE_LOG(LogFreyaProbe, Display, TEXT("Face animation before: %s"), *GetNameSafe(Anim ? Anim->GetClass() : nullptr));
        if (bDriveFaceDirectly)
        {
            UClass* Wanted = LoadClass<UAnimInstance>(nullptr, TEXT("/MetaHumanCharacter/Animation/ABP_MH_LiveLink.ABP_MH_LiveLink_C"));
            if (!Wanted)
            {
                UE_LOG(LogFreyaProbe, Error, TEXT("The MetaHuman Live Link animation blueprint could not be loaded."));
            }
            else if (!Anim || !Anim->IsA(Wanted))
            {
                Mesh->SetAnimInstanceClass(Wanted);
                Anim = Mesh->GetAnimInstance();
            }
        }
        if (!Anim) continue;
        FStructProperty* Subject = FindFProperty<FStructProperty>(Anim->GetClass(), TEXT("LLink_Face_Subj"));
        FNameProperty* Name = Subject ? FindFProperty<FNameProperty>(Subject->Struct, TEXT("Name")) : nullptr;
        if (!Name)
        {
            UE_LOG(LogFreyaProbe, Warning, TEXT("Face animation %s has no Live Link subject variable."), *GetNameSafe(Anim->GetClass()));
            continue;
        }
        void* Value = Subject->ContainerPtrToValuePtr<void>(Anim);
        const FName Before = Name->GetPropertyValue_InContainer(Value);
        if (bDriveFaceDirectly) Name->SetPropertyValue_InContainer(Value, SubjectName);
        UE_LOG(LogFreyaProbe, Display, TEXT("Face animation now: %s, subject was '%s', is '%s'."),
            *GetNameSafe(Anim->GetClass()), *Before.ToString(), *Name->GetPropertyValue_InContainer(Value).ToString());
    }
}

bool UFreyaAudioProbeComponent::StartFixture(const FString& Path)
{
    constexpr int64 Limit = int64(MaxFixtureSeconds) * FixtureSampleRate * sizeof(float);
    const FString AbsolutePath = FPaths::IsRelative(Path)
        ? FPaths::ConvertRelativePathToFull(FPaths::ProjectDir(), Path) : Path;
    TUniquePtr<FArchive> Reader(IFileManager::Get().CreateFileReader(*AbsolutePath));
    if (!Reader) return false;
    const int64 Size = Reader->TotalSize();
    if (Size <= 0 || Size > Limit || Size % sizeof(float) != 0) return false;
    TArray<uint8> Bytes;
    Bytes.SetNumUninitialized(static_cast<int32>(Size));
    Reader->Serialize(Bytes.GetData(), Size);
    if (Reader->IsError()) return false;
    TArray<float> Samples;
    Samples.SetNumUninitialized(Bytes.Num() / sizeof(float));
    FMemory::Memcpy(Samples.GetData(), Bytes.GetData(), Bytes.Num());
    for (float Sample : Samples)
        if (!FMath::IsFinite(Sample) || FMath::Abs(Sample) > 1.0f) return false;

    TArray<int16> Pcm;
    Pcm.SetNumUninitialized(Samples.Num());
    for (int32 Index = 0; Index < Samples.Num(); ++Index)
        Pcm[Index] = static_cast<int16>(FMath::Clamp(FMath::RoundToInt(Samples[Index] * 32767.0f), -32768, 32767));
    const double Seconds = Samples.Num() / double(FixtureSampleRate);

    if (!BeginSpeech(FixtureSampleRate, bPlayAudibly ? Volume : 0.0f)) return false;
    PushSpeech(Pcm.GetData(), Pcm.Num());
    EndSpeech();

    PeakCurve = 0.0f;
    CurvesAtPeak = 0;
    FixtureSeconds = Seconds;
    FixtureStartedAt = FPlatformTime::Seconds();
    LastReportAt = FixtureStartedAt;
    bFixtureActive = true;
    ShotsTaken = 0;
    bTakeShots = FParse::Param(FCommandLine::Get(), TEXT("FreyaShots"));
    UE_LOG(LogFreyaProbe, Display, TEXT("Fixture started: %.2f s, solver frames so far %d."),
        Seconds, GetSolverFrameCount());
    return true;
}

// Begin one utterance. The sound is given to the solver at full level; Loudness
// applies only to what is heard.
bool UFreyaAudioProbeComponent::BeginSpeech(int32 SampleRate, float Loudness)
{
    // The solver is fed at one rate. Anything else is refused, not resampled.
    if (SampleRate != FixtureSampleRate || !GetOwner() || !InitializeProbe()) return false;
    // A replacement sentence cancels an unfinished observation. Never calibrate
    // from a sentence whose playback or jaw tail was cut short.
    StopAudiblePlayback();
    Source->Cancel();
    ConfigureFace();

    Wave = NewObject<USoundWaveProcedural>(this);
    Wave->SetSampleRate(FixtureSampleRate);
    Wave->NumChannels = 1;
    Wave->Duration = INDEFINITELY_LOOPING_DURATION;
    Wave->SoundGroup = SOUNDGROUP_Voice;
    Wave->bLooping = false;
    SpeechVolume = FMath::Clamp(Loudness, 0.0f, 1.0f);
    SpeechSamples = 0;
    SpeechStartedAt = 0.0;
    SpeechTiming.Reset();
    bVoiceStarted = false;
    bSpeechOpen = true;
    LoudnessBins.Reset();
    JawTimes.Reset();
    JawValues.Reset();
    FirstFedAt = 0.0;
    return true;
}

void UFreyaAudioProbeComponent::PushSpeech(const int16* Samples, int32 Count)
{
    // At most what Freya ever sends for one utterance.
    if (!bSpeechOpen || bSpeechInputEnded || !Samples || Count <= 0 || !Source.IsValid() || !Wave) return;
    if (SpeechSamples + Count > int64(MaxFixtureSeconds) * FixtureSampleRate) return;

    TArray<float> Floats;
    Floats.SetNumUninitialized(Count);
    for (int32 Index = 0; Index < Count; ++Index) Floats[Index] = Samples[Index] / 32768.0f;
    Source->Push(Floats.GetData(), Count);
    for (int32 Index = 0; Index < Count; ++Index)
    {
        const int32 Bin = static_cast<int32>((SpeechSamples + Index) / (FixtureSampleRate / 100));
        if (Bin >= LoudnessBins.Num()) LoudnessBins.AddZeroed(Bin - LoudnessBins.Num() + 1);
        LoudnessBins[Bin] = FMath::Max(LoudnessBins[Bin], FMath::Abs(Floats[Index]));
    }
    SpeechTiming.Queue(FPlatformTime::Seconds(), Count / double(FixtureSampleRate));
    Wave->QueueAudio(reinterpret_cast<const uint8*>(Samples), Count * sizeof(int16));
    if (SpeechSamples == 0) FirstFedAt = FPlatformTime::Seconds();
    SpeechSamples += Count;

    // The sound starts a moment after the solver is first fed, so lips and voice agree.
    if (!bVoiceStarted && GetWorld() && !GetWorld()->GetTimerManager().IsTimerActive(PlaybackTimer))
    {
        const float Delay = FMath::Clamp(AudibleDelayMilliseconds, 0, 1500) / 1000.0f;
        if (Delay <= 0.0f) BeginAudiblePlayback();
        else GetWorld()->GetTimerManager().SetTimer(PlaybackTimer, this,
            &UFreyaAudioProbeComponent::BeginAudiblePlayback, Delay, false);
    }
}

// The whole utterance has been given. A pause in arriving audio is not its end;
// only this is.
void UFreyaAudioProbeComponent::EndSpeech()
{
    if (bSpeechOpen) bSpeechInputEnded = true;
}

// True from the first sound queued until it has all been heard.
bool UFreyaAudioProbeComponent::IsSpeechAudible() const
{
    if (!bSpeechOpen || SpeechSamples <= 0) return false;
    if (!bVoiceStarted) return true;
    return (Wave && Wave->GetAvailableAudioByteCount() > 0)
        || FPlatformTime::Seconds() < SpeechTiming.EndAt + 0.25;
}

void UFreyaAudioProbeComponent::BeginAudiblePlayback()
{
    if (!bSpeechOpen || !Wave || !GetOwner()) return;
    bVoiceStarted = true;
    SpeechStartedAt = FPlatformTime::Seconds();
    SpeechTiming.Start(SpeechStartedAt);

    if (!Voice)
    {
        Voice = NewObject<UAudioComponent>(GetOwner());
        Voice->bAutoDestroy = false;
        Voice->bAllowSpatialization = false;
        Voice->bIsUISound = true;
        Voice->RegisterComponent();
    }
    Voice->SetSound(Wave);
    // Never exactly zero: Unreal stops a silent sound, and its timing would be lost.
    Voice->SetVolumeMultiplier(FMath::Max(SpeechVolume, 0.0001f));
    Voice->Play();
}

void UFreyaAudioProbeComponent::StopAudiblePlayback()
{
    if (GetWorld()) GetWorld()->GetTimerManager().ClearTimer(PlaybackTimer);
    if (Voice) Voice->Stop();
    if (Wave) Wave->ResetAudio();
    bSpeechOpen = false;
    bSpeechInputEnded = false;
    bVoiceStarted = false;
    SpeechSamples = 0;
}

void UFreyaAudioProbeComponent::CancelSpeech()
{
    if (GetWorld()) GetWorld()->GetTimerManager().ClearTimer(AutoStartTimer);
    if (Source.IsValid()) Source->Cancel();
    StopAudiblePlayback();
    if (bFixtureActive)
    {
        UE_LOG(LogFreyaProbe, Display, TEXT("Speech cancelled after %.2f s."),
            FPlatformTime::Seconds() - FixtureStartedAt);
        Report(true);
    }
}

int32 UFreyaAudioProbeComponent::GetSolverFrameCount() const
{
    return Source.IsValid() ? Source->SolverFrames() : 0;
}

void UFreyaAudioProbeComponent::TickComponent(float DeltaTime, ELevelTick TickType,
    FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
    if (Link.IsValid()) Link->Tick();
    TrackSync();
    if (!bFixtureActive) return;

    // Read what the subject is publishing, the same values the face would use.
    if (IModularFeatures::Get().IsModularFeatureAvailable(ILiveLinkClient::ModularFeatureName))
    {
        ILiveLinkClient& Client = IModularFeatures::Get().GetModularFeature<ILiveLinkClient>(ILiveLinkClient::ModularFeatureName);
        FLiveLinkSubjectFrameData Frame;
        if (Client.EvaluateFrame_AnyThread(SubjectName, ULiveLinkBasicRole::StaticClass(), Frame))
        {
            if (const FLiveLinkBaseFrameData* Base = Frame.FrameData.GetBaseData())
            {
                float Largest = 0.0f;
                for (const float Value : Base->PropertyValues) Largest = FMath::Max(Largest, FMath::Abs(Value));
                if (Largest > PeakCurve) { PeakCurve = Largest; CurvesAtPeak = Base->PropertyValues.Num(); }
            }
        }
    }

    const double Now = FPlatformTime::Seconds();
    // With -FreyaShots, save a picture of what is on screen at a few moments, as
    // evidence of what the face was doing. Nothing else is captured.
    if (bTakeShots && ShotsTaken < 4 && Now - FixtureStartedAt >= 1.5 + 2.5 * ShotsTaken)
    {
        FScreenshotRequest::RequestScreenshot(
            FPaths::ProjectSavedDir() / FString::Printf(TEXT("Screenshots/probe_%d.png"), ShotsTaken), false, false);
        ++ShotsTaken;
    }
    if (Now - FixtureStartedAt > FixtureSeconds + TailSeconds) Report(true);
    else if (Now - LastReportAt >= 2.0) { LastReportAt = Now; Report(false); }
}

// How far the lips are behind the voice, measured rather than judged: the jaw's
// opening is compared with the loudness of the sound at every delay from half a
// second early to a second late, and the best match is the answer.
void UFreyaAudioProbeComponent::TrackSync()
{
    if (!bSpeechOpen || SpeechSamples <= 0 || LoudnessBins.Num() == 0) return;
    const double Now = FPlatformTime::Seconds();

    if (IModularFeatures::Get().IsModularFeatureAvailable(ILiveLinkClient::ModularFeatureName))
    {
        ILiveLinkClient& Client = IModularFeatures::Get().GetModularFeature<ILiveLinkClient>(ILiveLinkClient::ModularFeatureName);
        FLiveLinkSubjectFrameData Frame;
        if (Client.EvaluateFrame_AnyThread(SubjectName, ULiveLinkBasicRole::StaticClass(), Frame))
        {
            const FLiveLinkBaseStaticData* Static = Frame.StaticData.GetBaseData();
            const FLiveLinkBaseFrameData* Base = Frame.FrameData.GetBaseData();
            if (Static && Base)
            {
                if (JawIndex < 0 || JawIndex >= Static->PropertyNames.Num())
                {
                    JawIndex = Static->PropertyNames.IndexOfByPredicate([](const FName& Name)
                    { return Name.ToString().Contains(TEXT("jawOpen"), ESearchCase::IgnoreCase); });
                    UE_LOG(LogFreyaProbe, Display, TEXT("Jaw curve for timing: %s"),
                        JawIndex >= 0 ? *Static->PropertyNames[JawIndex].ToString() : TEXT("none found"));
                }
                if (JawIndex >= 0 && JawIndex < Base->PropertyValues.Num() && JawTimes.Num() < 20000)
                {
                    JawTimes.Add(Now);
                    JawValues.Add(Base->PropertyValues[JawIndex]);
                }
            }
        }
    }

    // Measured once all of the sentence has arrived and the jaw has had time to
    // finish it. Stopping when the sound stops would leave out the late frames,
    // which are the ones that show a lag.
    if (!bSpeechInputEnded || !bVoiceStarted) return;
    if (!Wave) return;
    SpeechTiming.ObserveQueue(Now, Wave->GetAvailableAudioByteCount() > 0);
    if (!SpeechTiming.Ready(Now, SyncTailSeconds)) return;
    MeasureSync();
}

void UFreyaAudioProbeComponent::MeasureSync()
{
    // Once per utterance, whether or not there was enough to measure.
    bSpeechInputEnded = false;
    // A delivery gap breaks the continuous sample timeline used below. Discard
    // that observation instead of mistaking compressed gaps for a lip delay.
    if (!bVoiceStarted || !SpeechTiming.bContinuous || JawTimes.Num() < 30 || LoudnessBins.Num() < 50)
    {
        JawTimes.Reset();
        JawValues.Reset();
        LoudnessBins.Reset();
        return;
    }

    // With -FreyaDump, write what was recorded, for exact analysis outside Unreal.
    if (FParse::Param(FCommandLine::Get(), TEXT("FreyaDump")))
    {
        FString Jaw = TEXT("seconds_after_sound_started,jaw_open\n");
        for (int32 Index = 0; Index < JawTimes.Num(); ++Index)
            Jaw += FString::Printf(TEXT("%.4f,%.5f\n"), JawTimes[Index] - SpeechStartedAt, JawValues[Index]);
        FFileHelper::SaveStringToFile(Jaw, *(FPaths::ProjectSavedDir() / TEXT("sync_jaw.csv")));
        FString Loud = TEXT("seconds_into_clip,loudness\n");
        for (int32 Index = 0; Index < LoudnessBins.Num(); ++Index)
            Loud += FString::Printf(TEXT("%.2f,%.5f\n"), Index / 100.0, LoudnessBins[Index]);
        FFileHelper::SaveStringToFile(Loud, *(FPaths::ProjectSavedDir() / TEXT("sync_loudness.csv")));
        UE_LOG(LogFreyaProbe, Display, TEXT("SYNC: recorded %d jaw samples; sound started %.0f ms after the solver was first fed."),
            JawTimes.Num(), (SpeechStartedAt - FirstFedAt) * 1000.0);
    }

    // The utterance has finished sounding. Find the delay that fits best.
    const auto LoudnessAt = [this](double Seconds) -> float
    {
        const int32 Bin = FMath::FloorToInt(Seconds * 100.0);
        return LoudnessBins.IsValidIndex(Bin) ? LoudnessBins[Bin] : 0.0f;
    };
    double BestScore = -1.0;
    int32 BestDelay = 0;
    for (int32 Delay = -500; Delay <= 1000; Delay += 10)
    {
        double SumXY = 0, SumX = 0, SumY = 0, SumXX = 0, SumYY = 0;
        const int32 Count = JawTimes.Num();
        for (int32 Index = 0; Index < Count; ++Index)
        {
            const double X = JawValues[Index];
            const double Y = LoudnessAt(JawTimes[Index] - SpeechStartedAt - Delay / 1000.0);
            SumXY += X * Y; SumX += X; SumY += Y; SumXX += X * X; SumYY += Y * Y;
        }
        const double Denominator = FMath::Sqrt((Count * SumXX - SumX * SumX) * (Count * SumYY - SumY * SumY));
        const double Score = Denominator > KINDA_SMALL_NUMBER ? (Count * SumXY - SumX * SumY) / Denominator : 0.0;
        if (Score > BestScore) { BestScore = Score; BestDelay = Delay; }
    }
    // A plainer check beside it: when the first sound is, and when the jaw first opens.
    int32 FirstLoud = LoudnessBins.IndexOfByPredicate([](float Value) { return Value > 0.05f; });
    int32 FirstOpen = JawValues.IndexOfByPredicate([](float Value) { return Value > 0.08f; });
    if (FirstLoud >= 0 && FirstOpen >= 0)
        UE_LOG(LogFreyaProbe, Display, TEXT("SYNC onset: first sound %d ms into the clip; jaw first opened %.0f ms after the sound started playing; so the jaw is %.0f ms %s."),
            FirstLoud * 10, (JawTimes[FirstOpen] - SpeechStartedAt) * 1000.0,
            FMath::Abs((JawTimes[FirstOpen] - SpeechStartedAt) * 1000.0 - FirstLoud * 10),
            (JawTimes[FirstOpen] - SpeechStartedAt) * 1000.0 >= FirstLoud * 10 ? TEXT("late") : TEXT("early"));
    // And the same at the other end, which shows whether the lips fall behind as she goes on.
    int32 LastLoud = LoudnessBins.FindLastByPredicate([](float Value) { return Value > 0.05f; });
    int32 LastOpen = JawValues.FindLastByPredicate([](float Value) { return Value > 0.08f; });
    if (LastLoud >= 0 && LastOpen >= 0)
        UE_LOG(LogFreyaProbe, Display, TEXT("SYNC end: last sound %d ms into the clip; jaw last open %.0f ms after the sound started playing; difference %.0f ms."),
            LastLoud * 10, (JawTimes[LastOpen] - SpeechStartedAt) * 1000.0,
            (JawTimes[LastOpen] - SpeechStartedAt) * 1000.0 - LastLoud * 10);
    const double Seconds = JawTimes.Last() - JawTimes[0];
    UE_LOG(LogFreyaProbe, Display,
        TEXT("SYNC: lips %s voice by %d ms (match %.2f) over %.1f s at %.0f frames a second; sound started %.0f ms after the solver was first fed."),
        BestDelay >= 0 ? TEXT("behind") : TEXT("ahead of"), FMath::Abs(BestDelay), BestScore, Seconds,
        Seconds > 0 ? JawTimes.Num() / Seconds : 0.0, (SpeechStartedAt - FirstFedAt) * 1000.0);
    LastLipDelayMilliseconds = BestDelay;
    LastLipMatch = static_cast<float>(BestScore);
    // When the measurement is trustworthy, move the sound's delay most of the way
    // toward where the lips would be a touch ahead of the voice, which the eye
    // forgives more readily than the reverse. It settles over a few sentences.
    if (bCalibrateDelay && BestScore >= 0.45 && Seconds >= 1.0)
    {
        const int32 Before = AudibleDelayMilliseconds;
        AudibleDelayMilliseconds = FMath::Clamp(
            AudibleDelayMilliseconds + FMath::RoundToInt(0.6f * (BestDelay + 30)), 0, 1500);
        UE_LOG(LogFreyaProbe, Display, TEXT("SYNC: sound delay changed from %d to %d ms."), Before, AudibleDelayMilliseconds);
    }
    // Measured once per utterance.
    JawTimes.Reset();
    JawValues.Reset();
    LoudnessBins.Reset();
}

void UFreyaAudioProbeComponent::Report(bool bFinal)
{
    const int32 Frames = GetSolverFrameCount();
    if (!bFinal)
    {
        UE_LOG(LogFreyaProbe, Display, TEXT("Progress: %.1f s, solver frames %d, peak curve %.3f over %d curves."),
            FPlatformTime::Seconds() - FixtureStartedAt, Frames, PeakCurve, CurvesAtPeak);
        return;
    }
    bFixtureActive = false;
    // One line that can be searched for in the log of an Editor or a packaged run.
    const TCHAR* Verdict = Frames > 0 && PeakCurve > 0.05f ? TEXT("SOLVER PRODUCED ANIMATION")
        : Frames > 0 ? TEXT("SOLVER RAN BUT CURVES STAYED NEAR ZERO") : TEXT("NO SOLVER OUTPUT");
    UE_LOG(LogFreyaProbe, Display, TEXT("RESULT: %s. solver frames %d, peak curve %.3f over %d curves, fixture %.2f s."),
        Verdict, Frames, PeakCurve, CurvesAtPeak, FixtureSeconds);
    // With -FreyaQuit an unattended test run ends by itself once it has its result.
    if (FParse::Param(FCommandLine::Get(), TEXT("FreyaQuit")) && GetWorld())
    {
        FTimerHandle Unused;
        GetWorld()->GetTimerManager().SetTimer(Unused, FTimerDelegate::CreateWeakLambda(this, [this]()
        {
            UKismetSystemLibrary::QuitGame(this, nullptr, EQuitPreference::Quit, false);
        // Long enough for the lip timing to be measured first.
        }), 3.0f, false);
    }
}

void UFreyaAudioProbeComponent::StopProbe()
{
    CancelSpeech();
    if (SourceId.IsValid() && IModularFeatures::Get().IsModularFeatureAvailable(ILiveLinkClient::ModularFeatureName))
        IModularFeatures::Get().GetModularFeature<ILiveLinkClient>(ILiveLinkClient::ModularFeatureName).RemoveSource(SourceId);
    Source.Reset();
    SourceId.Invalidate();
}

void UFreyaAudioProbeComponent::EndPlay(const EEndPlayReason::Type Reason)
{
    if (Link.IsValid()) { Link->Stop(); Link.Reset(); }
    StopProbe();
    Super::EndPlay(Reason);
}
