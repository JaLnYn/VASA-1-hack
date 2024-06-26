# from datasets import VideoMAE # TBD - https://github.com/search?q=VideoMAE+voxceleb&type=code
# voxceleb2 dataset here - https://github.com/johndpope/VASA-1-hack/issues/5#issuecomment-2077007921

import torch
from torch.utils.data import DataLoader
from torch import optim, nn
from transformers import get_cosine_schedule_with_warmup
from Net import FaceEncoder, FaceDecoder, DiffusionTransformer,DisentanglementLosses
import torchvision.transforms as transforms
from FaceHelper import FaceHelper
from modules.real3d.facev2v_warp.network import AppearanceFeatureExtractor, CanonicalKeypointDetector, PoseExpressionEstimator, MotionFieldEstimator, Generator
from mae import VideoMAE

# Training configuration
num_epochs_stage1 = 100
num_epochs_stage2 = 100
batch_size = 32
learning_rate = 0.001

# Initialize models
encoder = FaceEncoder()
decoder = FaceDecoder()
fh = FaceHelper()
diffusion_transformer = DiffusionTransformer(num_layers=6, num_heads=8, hidden_size=512)
motion_field_estimator = MotionFieldEstimator(model_scale='small')

# Data loading and transformations
transform = transforms.Compose([
    transforms.Resize((256, 256)),
    transforms.ToTensor(),
])

dataset = VideoMAE(
    root='/path/to/voxceleb2/root',
    setting='/path/to/voxceleb2/train.txt',
    train=True,
    image_size=256,
    audio_conf={
        'num_mel_bins': 128,
        'target_length': 1024,
        'freqm': 0,
        'timem': 0,
        'noise': False,
        'mean': -4.6476,  
        'std': 4.5699,
    },
)

dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=4)
disentanglement_losses = DisentanglementLosses()
def compute_lip_sync_loss(original_landmarks, generated_landmarks):
    loss_fn = torch.nn.MSELoss()
    return loss_fn(original_landmarks, generated_landmarks)

# Stage 1: Face Latent Space Construction
print("Stage 1: Face Latent Space Construction")
params_stage1 = list(encoder.parameters()) + list(decoder.parameters())
optimizer_stage1 = optim.Adam(params_stage1, lr=learning_rate, weight_decay=1e-5)
scheduler_stage1 = get_cosine_schedule_with_warmup(optimizer_stage1, num_warmup_steps=0, num_training_steps=len(dataloader) * num_epochs_stage1)
for epoch in range(num_epochs_stage1):
    for batch in dataloader:
        video_frames, _, _, _ = batch
        video_frames = video_frames.cuda()
        
        for frame_idx in range(0, video_frames.shape[1], 2):  # Process frames in pairs
            frame1 = video_frames[:, frame_idx]
            frame2 = video_frames[:, frame_idx + 1]
            
            # Forward pass through encoder and decoder for frame1
            appearance_volume1, identity_code1, head_pose1, facial_dynamics1 = encoder(frame1)
            reconstructed_face1 = decoder(appearance_volume1, identity_code1, head_pose1, facial_dynamics1)
            
            # Forward pass through encoder and decoder for frame2
            appearance_volume2, identity_code2, head_pose2, facial_dynamics2 = encoder(frame2)
            reconstructed_face2 = decoder(appearance_volume2, identity_code2, head_pose2, facial_dynamics2)
            
            # Pairwise head pose and facial dynamics transfer
            pose_transfer_face1 = decoder(appearance_volume1, identity_code1, head_pose2, facial_dynamics1)
            dyn_transfer_face2 = decoder(appearance_volume2, identity_code2, head_pose2, facial_dynamics1)
            
            # Cross-identity pose and facial motion transfer
            cross_id_transfer_face1 = decoder(appearance_volume1, identity_code2, head_pose1, facial_dynamics1)
            cross_id_transfer_face2 = decoder(appearance_volume2, identity_code1, head_pose2, facial_dynamics2)
            
            # Compute disentanglement losses
            loss_pairwise_transfer, loss_id_sim = disentanglement_losses(
                frame1, frame2, reconstructed_face1, reconstructed_face2,
                pose_transfer_face1, dyn_transfer_face2,
                cross_id_transfer_face1, cross_id_transfer_face2
            )
            
            # Reconstruction loss
            reconstruction_loss = nn.L1Loss()(frame1, reconstructed_face1) + nn.L1Loss()(frame2, reconstructed_face2)
            
            # Total loss
            total_loss = reconstruction_loss + loss_pairwise_transfer + loss_id_sim
            
            # Optimization step
            optimizer_stage1.zero_grad()
            total_loss.backward()
            optimizer_stage1.step()
            scheduler_stage1.step()
    
    print(f"Stage 1 - Epoch [{epoch+1}/{num_epochs_stage1}], Total Loss: {total_loss.item()}, Reconstruction Loss: {reconstruction_loss.item()}, Pairwise Transfer Loss: {loss_pairwise_transfer.item()}, Identity Similarity Loss: {loss_id_sim.item()}")
    
    # Save the encoder and decoder models
    torch.save(encoder.state_dict(), f"encoder_stage1_epoch{epoch+1}.pth")
    torch.save(decoder.state_dict(), f"decoder_stage1_epoch{epoch+1}.pth")

# Stage 2: Holistic Facial Dynamics Generation
print("Stage 2: Holistic Facial Dynamics Generation")
params_stage2 = list(diffusion_transformer.parameters())
optimizer_stage2 = optim.Adam(params_stage2, lr=learning_rate, weight_decay=1e-5)
scheduler_stage2 = get_cosine_schedule_with_warmup(optimizer_stage2, num_warmup_steps=0, num_training_steps=len(dataloader) * num_epochs_stage2)

guidance_scale = 1.5  # Set the guidance scale for CFG

for epoch in range(num_epochs_stage2):
    for batch in dataloader:
        video_frames, _, audio_features, _ = batch
        video_frames = video_frames.cuda()
        audio_features = audio_features.cuda()
        
        for frame_idx in range(video_frames.shape[1]):
            frame = video_frames[:, frame_idx]
            
            # Forward pass through encoder
            appearance_volume, identity_code, head_pose, facial_dynamics = encoder(frame)
            
            # Generate dynamics using structured inputs
            gaze_direction = fh.estimate_gaze(frame)
            head_distance = fh.head_distance_estimator(frame)  
            emotion_offset = fh.detect_emotions(frame)
            
            # Diffusion transformer for generating dynamics with CFG
            generated_dynamics = diffusion_transformer(
                facial_dynamics, audio_features[:, frame_idx], gaze_direction, head_distance, emotion_offset, guidance_scale=guidance_scale)
            
            # Generate motion field using the MotionFieldEstimator
            deformation, occlusion = motion_field_estimator(appearance_volume, head_pose, generated_dynamics)
            
            # Face reconstruction using the modified FaceDecoder
            reconstructed_face = decoder(appearance_volume, identity_code, head_pose, generated_dynamics)
            
            # Get lip landmarks for original and reconstructed face
            original_lip_landmarks = fh.mediapipe_lip_landmark_detector(frame)
            generated_lip_landmarks = fh.mediapipe_lip_landmark_detector(reconstructed_face.detach())
            
            # Compute lip sync loss
            lip_sync_loss = compute_lip_sync_loss(original_lip_landmarks, generated_lip_landmarks)
            
            # Optimization step
            optimizer_stage2.zero_grad()
            lip_sync_loss.backward()
            optimizer_stage2.step()
            scheduler_stage2.step()
        
        print(f"Stage 2 - Epoch [{epoch+1}/{num_epochs_stage2}], Lip Sync Loss: {lip_sync_loss.item()}")
    
    # Save the diffusion transformer model
    torch.save(diffusion_transformer.state_dict(), f"diffusion_transformer_stage2_epoch{epoch+1}.pth")
