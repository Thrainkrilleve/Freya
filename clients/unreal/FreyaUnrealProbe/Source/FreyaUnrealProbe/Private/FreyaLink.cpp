#include "FreyaLink.h"
#include "FreyaAudioProbeComponent.h"
#include "IWebSocket.h"
#include "WebSocketsModule.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Engine/Engine.h"
#include "Engine/GameViewportClient.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "Styling/CoreStyle.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/SOverlay.h"
#include "Widgets/Input/SEditableTextBox.h"
#include "Widgets/Input/SMultiLineEditableTextBox.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/Text/STextBlock.h"

DEFINE_LOG_CATEGORY_STATIC(LogFreyaLink, Log, All);

namespace
{
    constexpr int32 ProtocolVersion = 1;
    // One message of speech: "FSP1", the utterance, the frame's number, then samples.
    constexpr int32 FrameHeaderBytes = 12;
    constexpr int32 MaxFrameBytes = 64 * 1024;
    constexpr int32 MaxLines = 200;
    constexpr int32 MaxTextCharacters = 4000;
    constexpr int32 MaxAttempts = 5;
}

FFreyaLink::FFreyaLink(UFreyaAudioProbeComponent* InOwner, int32 InPort, const FString& InKey)
    : Owner(InOwner), Port(InPort), Key(InKey)
{
}

FFreyaLink::~FFreyaLink()
{
    Stop();
}

void FFreyaLink::Start()
{
    BuildWidget();
    SetStatus(TEXT("Connecting to Freya..."));
    Connect();
}

void FFreyaLink::Stop()
{
    bStopped = true;
    if (Socket.IsValid())
    {
        Socket->OnConnected().Clear();
        Socket->OnConnectionError().Clear();
        Socket->OnClosed().Clear();
        Socket->OnMessage().Clear();
        Socket->OnBinaryMessage().Clear();
        if (Socket->IsConnected()) Socket->Close();
        Socket.Reset();
    }
    if (Root.IsValid() && GEngine && GEngine->GameViewport)
        GEngine->GameViewport->RemoveViewportWidgetContent(Root.ToSharedRef());
    Root.Reset();
}

void FFreyaLink::Connect()
{
    if (bStopped) return;
    if (!FModuleManager::Get().IsModuleLoaded(TEXT("WebSockets")))
        FModuleManager::Get().LoadModule(TEXT("WebSockets"));

    // The key goes in a header, never in the address. Once used it is replaced
    // by a session, which is what a reconnection presents.
    TMap<FString, FString> Headers;
    Headers.Add(TEXT("Authorization"),
        Session.IsEmpty() ? FString::Printf(TEXT("FreyaPair %s"), *Key) : FString::Printf(TEXT("FreyaSession %s"), *Session));
    Socket = FWebSocketsModule::Get().CreateWebSocket(
        FString::Printf(TEXT("ws://127.0.0.1:%d/ws"), Port), FString(), Headers);
    bWelcomed = false;

    TWeakPtr<FFreyaLink> Weak = AsShared();
    Socket->OnConnected().AddLambda([Weak]()
    {
        if (TSharedPtr<FFreyaLink> Self = Weak.Pin())
        {
            Self->Attempts = 0;
            TSharedRef<FJsonObject> Hello = MakeShared<FJsonObject>();
            Hello->SetStringField(TEXT("type"), TEXT("client.hello"));
            Hello->SetNumberField(TEXT("protocol"), ProtocolVersion);
            TSharedRef<FJsonObject> Client = MakeShared<FJsonObject>();
            Client->SetStringField(TEXT("kind"), TEXT("unreal"));
            Client->SetStringField(TEXT("name"), TEXT("FreyaUnrealProbe"));
            Client->SetStringField(TEXT("version"), TEXT("0.1"));
            Hello->SetObjectField(TEXT("client"), Client);
            Hello->SetArrayField(TEXT("features"), { MakeShared<FJsonValueString>(TEXT("speech.playback")) });
            Self->SendJson(Hello);
        }
    });
    Socket->OnConnectionError().AddLambda([Weak](const FString& Error)
    {
        if (TSharedPtr<FFreyaLink> Self = Weak.Pin())
        {
            UE_LOG(LogFreyaLink, Warning, TEXT("Could not connect to Freya: %s"), *Error);
            Self->SetStatus(TEXT("Freya is not answering."));
            Self->NextConnectAt = FPlatformTime::Seconds() + FMath::Min(2.0 * (Self->Attempts + 1), 10.0);
        }
    });
    Socket->OnClosed().AddLambda([Weak](int32, const FString&, bool)
    {
        if (TSharedPtr<FFreyaLink> Self = Weak.Pin())
        {
            // Whatever was being said is over as far as this window knows.
            if (UFreyaAudioProbeComponent* Probe = Self->Owner.Get()) Probe->CancelSpeech();
            Self->bUtteranceOpen = false;
            Self->SetStatus(TEXT("Disconnected from Freya."));
            Self->NextConnectAt = FPlatformTime::Seconds() + FMath::Min(2.0 * (Self->Attempts + 1), 10.0);
        }
    });
    Socket->OnMessage().AddLambda([Weak](const FString& Message)
    {
        if (TSharedPtr<FFreyaLink> Self = Weak.Pin()) Self->HandleText(Message);
    });
    Socket->OnBinaryMessage().AddLambda([Weak](const void* Data, SIZE_T Size, bool bIsLastFragment)
    {
        if (TSharedPtr<FFreyaLink> Self = Weak.Pin()) Self->HandleBinary(Data, Size, bIsLastFragment);
    });
    Socket->Connect();
}

void FFreyaLink::Tick()
{
    if (bStopped) return;

    // Tell Freya when the sentence has actually finished sounding. Until then
    // she counts herself as speaking and keeps her microphone ignoring it.
    if (bUtteranceOpen && bUtteranceEnded)
    {
        UFreyaAudioProbeComponent* Probe = Owner.Get();
        if (!Probe || !Probe->IsSpeechAudible())
        {
            bUtteranceOpen = false;
            UE_LOG(LogFreyaLink, Display, TEXT("Utterance %u finished sounding; telling Freya."), Utterance);
            TSharedRef<FJsonObject> Done = MakeShared<FJsonObject>();
            Done->SetStringField(TEXT("type"), TEXT("speech.finished"));
            Done->SetNumberField(TEXT("utterance"), Utterance);
            SendJson(Done);
        }
    }

    // A few tries to reconnect, further apart each time, then stop trying.
    if (NextConnectAt > 0.0 && FPlatformTime::Seconds() >= NextConnectAt)
    {
        NextConnectAt = 0.0;
        if (Attempts < MaxAttempts && !Session.IsEmpty())
        {
            ++Attempts;
            SetStatus(TEXT("Reconnecting to Freya..."));
            Connect();
        }
        else
        {
            SetStatus(TEXT("Not connected to Freya. Start this window from Freya to connect."));
        }
    }
}

void FFreyaLink::SendJson(const TSharedRef<FJsonObject>& Object)
{
    if (!Socket.IsValid() || !Socket->IsConnected()) return;
    FString Out;
    TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
        TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Out);
    if (FJsonSerializer::Serialize(Object, Writer)) Socket->Send(Out);
}

void FFreyaLink::Say(const FString& Text)
{
    const FString Trimmed = Text.TrimStartAndEnd().Left(MaxTextCharacters);
    if (Trimmed.IsEmpty()) return;
    if (!Socket.IsValid() || !Socket->IsConnected())
    {
        SetStatus(TEXT("Not connected to Freya; that was not sent."));
        return;
    }
    TSharedRef<FJsonObject> Message = MakeShared<FJsonObject>();
    // A line starting with a slash is one of Freya's commands, as in her own window.
    Message->SetStringField(TEXT("type"), Trimmed.StartsWith(TEXT("/")) ? TEXT("command") : TEXT("say"));
    Message->SetStringField(TEXT("text"), Trimmed);
    SendJson(Message);
}

void FFreyaLink::HandleText(const FString& Message)
{
    TSharedPtr<FJsonObject> Event;
    const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Message);
    if (!FJsonSerializer::Deserialize(Reader, Event) || !Event.IsValid()) return;
    HandleEvent(Event);
}

void FFreyaLink::HandleEvent(const TSharedPtr<FJsonObject>& Event)
{
    const FString Type = Event->GetStringField(TEXT("type"));
    UFreyaAudioProbeComponent* Probe = Owner.Get();

    if (Type == TEXT("hello"))
    {
        // What was said before this window connected.
        Lines.Reset();
        const TArray<TSharedPtr<FJsonValue>>* History = nullptr;
        if (Event->TryGetArrayField(TEXT("history"), History))
            for (const TSharedPtr<FJsonValue>& Item : *History)
                if (Item.IsValid() && Item->Type == EJson::Object) HandleEvent(Item->AsObject());
    }
    else if (Type == TEXT("welcome"))
    {
        bWelcomed = true;
        Event->TryGetStringField(TEXT("session"), Session);
        bool bPlayback = false;
        const TArray<TSharedPtr<FJsonValue>>* Features = nullptr;
        if (Event->TryGetArrayField(TEXT("features"), Features))
            for (const TSharedPtr<FJsonValue>& Feature : *Features)
                bPlayback |= Feature.IsValid() && Feature->AsString() == TEXT("speech.playback");
        SetStatus(bPlayback ? TEXT("Connected to Freya.")
            : TEXT("Connected to Freya. Her voice is playing somewhere else."));
        UE_LOG(LogFreyaLink, Display, TEXT("Welcomed by Freya; playback %s."), bPlayback ? TEXT("granted") : TEXT("not granted"));
    }
    else if (Type == TEXT("refused"))
    {
        FString Reason;
        Event->TryGetStringField(TEXT("reason"), Reason);
        SetStatus(FString::Printf(TEXT("Freya refused this window: %s"), *Reason.Left(200)));
        Attempts = MaxAttempts;
    }
    else if (Type == TEXT("user"))
    {
        bReplying = false;
        AddLine(TEXT("You"), Event->GetStringField(TEXT("text")));
    }
    else if (Type == TEXT("assistant_start"))
    {
        bReplying = true;
        Reply.Reset();
        RefreshTranscript();
    }
    else if (Type == TEXT("assistant_chunk"))
    {
        bReplying = true;
        Reply += Event->GetStringField(TEXT("text"));
        RefreshTranscript();
    }
    else if (Type == TEXT("assistant_end"))
    {
        if (bReplying && !Reply.TrimStartAndEnd().IsEmpty()) AddLine(TEXT("Freya"), Reply.TrimStartAndEnd());
        bReplying = false;
        Reply.Reset();
        RefreshTranscript();
    }
    else if (Type == TEXT("notice") || Type == TEXT("error"))
    {
        AddLine(TEXT(""), Event->GetStringField(TEXT("text")));
    }
    else if (Type == TEXT("speech.start"))
    {
        double Number = 0.0, Rate = 0.0, Volume = 1.0;
        Event->TryGetNumberField(TEXT("utterance"), Number);
        Event->TryGetNumberField(TEXT("sample_rate"), Rate);
        Event->TryGetNumberField(TEXT("volume"), Volume);
        Utterance = static_cast<uint32>(Number);
        bUtteranceEnded = false;
        bUtteranceOpen = Probe && Probe->BeginSpeech(FMath::RoundToInt(Rate), static_cast<float>(Volume));
        UE_LOG(LogFreyaLink, Display, TEXT("Utterance %u from Freya: %d Hz, volume %.2f, %s."),
            Utterance, FMath::RoundToInt(Rate), Volume, bUtteranceOpen ? TEXT("playing") : TEXT("REFUSED"));
        if (!bUtteranceOpen)
        {
            // Not something this window can play: say so, so Freya does not wait.
            TSharedRef<FJsonObject> Failed = MakeShared<FJsonObject>();
            Failed->SetStringField(TEXT("type"), TEXT("speech.interrupted"));
            Failed->SetNumberField(TEXT("utterance"), Utterance);
            SendJson(Failed);
        }
    }
    else if (Type == TEXT("speech.end"))
    {
        double Number = 0.0;
        Event->TryGetNumberField(TEXT("utterance"), Number);
        if (bUtteranceOpen && static_cast<uint32>(Number) == Utterance)
        {
            bUtteranceEnded = true;
            if (Probe) Probe->EndSpeech();
        }
    }
    else if (Type == TEXT("speech.cancel"))
    {
        if (Probe) Probe->CancelSpeech();
        bUtteranceOpen = false;
    }
}

void FFreyaLink::HandleBinary(const void* Data, SIZE_T Size, bool bIsLastFragment)
{
    // A frame may arrive in pieces. Nothing larger than Freya sends is kept.
    if (Partial.Num() + static_cast<int64>(Size) > MaxFrameBytes) { Partial.Reset(); return; }
    Partial.Append(static_cast<const uint8*>(Data), static_cast<int32>(Size));
    if (!bIsLastFragment) return;

    TArray<uint8> Frame = MoveTemp(Partial);
    Partial.Reset();
    if (Frame.Num() <= FrameHeaderBytes || FMemory::Memcmp(Frame.GetData(), "FSP1", 4) != 0) return;
    uint32 FrameUtterance = 0;
    FMemory::Memcpy(&FrameUtterance, Frame.GetData() + 4, sizeof(uint32));
    if (!bUtteranceOpen || FrameUtterance != Utterance) return;

    const int32 Count = (Frame.Num() - FrameHeaderBytes) / sizeof(int16);
    if (UFreyaAudioProbeComponent* Probe = Owner.Get())
        Probe->PushSpeech(reinterpret_cast<const int16*>(Frame.GetData() + FrameHeaderBytes), Count);
}

// -- the chat over the scene ----------------------------------------------------------

void FFreyaLink::BuildWidget()
{
    if (!GEngine || !GEngine->GameViewport) return;

    const FSlateFontInfo Font = FCoreStyle::GetDefaultFontStyle("Regular", 11);
    TWeakPtr<FFreyaLink> Weak = AsShared();

    Root = SNew(SOverlay)
        + SOverlay::Slot().VAlign(VAlign_Bottom).HAlign(HAlign_Fill).Padding(10.0f)
        [
            SNew(SBorder)
            .BorderImage(FCoreStyle::Get().GetBrush("GenericWhiteBox"))
            .BorderBackgroundColor(FLinearColor(0.02f, 0.03f, 0.05f, 0.82f))
            .Padding(8.0f)
            [
                SNew(SVerticalBox)
                + SVerticalBox::Slot().AutoHeight().Padding(0.0f, 0.0f, 0.0f, 4.0f)
                [
                    SAssignNew(Status, STextBlock)
                    .Font(FCoreStyle::GetDefaultFontStyle("Regular", 9))
                    .ColorAndOpacity(FLinearColor(0.6f, 0.65f, 0.75f))
                ]
                + SVerticalBox::Slot().AutoHeight()
                [
                    // Read-only, so it can be selected and copied but not changed.
                    SNew(SBox).HeightOverride(170.0f)
                    [
                        SAssignNew(Transcript, SMultiLineEditableTextBox)
                        .IsReadOnly(true)
                        .AutoWrapText(true)
                        .AlwaysShowScrollbars(false)
                        .Font(Font)
                        .BackgroundColor(FLinearColor(0.0f, 0.0f, 0.0f, 0.0f))
                        .ForegroundColor(FLinearColor(0.92f, 0.93f, 0.95f))
                    ]
                ]
                + SVerticalBox::Slot().AutoHeight().Padding(0.0f, 6.0f, 0.0f, 0.0f)
                [
                    SAssignNew(Input, SEditableTextBox)
                    .Font(Font)
                    .HintText(FText::FromString(TEXT("Message Freya, then press Enter")))
                    .ClearKeyboardFocusOnCommit(false)
                    .OnTextCommitted_Lambda([Weak](const FText& Text, ETextCommit::Type How)
                    {
                        TSharedPtr<FFreyaLink> Self = Weak.Pin();
                        if (!Self.IsValid() || How != ETextCommit::OnEnter) return;
                        Self->Say(Text.ToString());
                        if (Self->Input.IsValid()) Self->Input->SetText(FText::GetEmpty());
                    })
                ]
            ]
        ];

    GEngine->GameViewport->AddViewportWidgetContent(Root.ToSharedRef(), 10);

    // A mouse pointer and typing into the box, not game controls.
    if (UFreyaAudioProbeComponent* Probe = Owner.Get())
        if (UWorld* World = Probe->GetWorld())
            if (APlayerController* Player = World->GetFirstPlayerController())
            {
                Player->bShowMouseCursor = true;
                FInputModeUIOnly Mode;
                Mode.SetLockMouseToViewportBehavior(EMouseLockMode::DoNotLock);
                if (Input.IsValid()) Mode.SetWidgetToFocus(Input);
                Player->SetInputMode(Mode);
            }
}

void FFreyaLink::AddLine(const FString& Who, const FString& Text)
{
    const FString Clean = Text.TrimStartAndEnd();
    if (Clean.IsEmpty()) return;
    Lines.Add(Who.IsEmpty() ? FString::Printf(TEXT("[%s]"), *Clean) : FString::Printf(TEXT("%s: %s"), *Who, *Clean));
    if (Lines.Num() > MaxLines) Lines.RemoveAt(0, Lines.Num() - MaxLines);
    RefreshTranscript();
}

void FFreyaLink::RefreshTranscript()
{
    if (!Transcript.IsValid()) return;
    FString All = FString::Join(Lines, TEXT("\n\n"));
    if (bReplying)
    {
        if (!All.IsEmpty()) All += TEXT("\n\n");
        All += TEXT("Freya: ") + Reply;
    }
    Transcript->SetText(FText::FromString(All));
    Transcript->GoTo(ETextLocation::EndOfDocument);
}

void FFreyaLink::SetStatus(const FString& Text)
{
    if (Status.IsValid()) Status->SetText(FText::FromString(Text));
}
