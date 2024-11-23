import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
from sklearn.metrics import accuracy_score, f1_score
from transformers import (
    AutoTokenizer, 
    AutoModelForCausalLM,
    AutoConfig,
    TrainingArguments,
    Trainer,
    get_scheduler
)
from datasets import load_dataset

# Constants
MODEL_ID = "state-spaces/mamba-130m-hf"
DATASET_NAME = "dair-ai/emotion"
NUM_LABELS = 6
SAVE_DIR = "./distilled_mamba_classifier"
EMOTION_LABELS = {
    0: "sadness",
    1: "joy",
    2: "love",
    3: "anger",
    4: "fear",
    5: "surprise"
}

# Device setup
def get_device():
    if torch.cuda.is_available():
        return torch.device("cuda")
    elif torch.backends.mps.is_available():
        return torch.device("cpu")
    else:
        return torch.device("cpu")

device = get_device()
print(f"Using device: {device}")

class MambaForSequenceClassification(nn.Module):
    def __init__(self, base_model, num_labels, pooling_type='mean'):
        super().__init__()
        self.mamba = base_model
        self.num_labels = num_labels
        self.pooling_type = pooling_type
        
        # Freeze most of the base model layers for fine-tuning
        # Only fine-tune the last few layers
        total_layers = len(list(base_model.parameters()))
        for param in list(base_model.parameters())[:(total_layers - 2)]:
            param.requires_grad = False
        
        # Get hidden size from model config
        self.hidden_size = self.mamba.config.d_model
        
        # Create projection layer
        self.projection = nn.Linear(self.mamba.config.vocab_size, self.hidden_size)
        
        # Classification head
        self.classifier = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size // 2),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(self.hidden_size // 2, num_labels)
        )
        
        print(f"\nModel initialized with:")
        print(f"- Hidden size: {self.hidden_size}")
        print(f"- Vocab size: {self.mamba.config.vocab_size}")
        print(f"- Number of layers: {self.mamba.config.n_layer}")
        print("- Fine-tuning last 2 layers of base model + classifier")
    
    def forward(self, input_ids, attention_mask=None, labels=None):
        # Get Mamba output
        outputs = self.mamba(input_ids, attention_mask=attention_mask)
        hidden_states = outputs.logits
        
        # Apply pooling
        if self.pooling_type == 'mean':
            if attention_mask is not None:
                mask_expanded = attention_mask.unsqueeze(-1).float()
                pooled_output = (hidden_states * mask_expanded).sum(dim=1) / mask_expanded.sum(dim=1).clamp(min=1e-9)
            else:
                pooled_output = hidden_states.mean(dim=1)
        else:  # max pooling
            if attention_mask is not None:
                mask_expanded = attention_mask.unsqueeze(-1).float()
                hidden_states = hidden_states * mask_expanded - 1e9 * (1 - mask_expanded)
            pooled_output = hidden_states.max(dim=1)[0]
        
        # Project and classify
        projected = self.projection(pooled_output)
        logits = self.classifier(projected)
        
        # Compute loss if needed
        loss = None
        if labels is not None:
            loss = F.cross_entropy(logits, labels)
        
        return type('MambaSequenceClassifierOutput', (), {
            'loss': loss,
            'logits': logits
        })

    @property
    def device(self):
        return next(self.parameters()).device
        
    def to(self, device):
        super().to(device)
        self.mamba = self.mamba.to(device)
        self.projection = self.projection.to(device)
        self.classifier = self.classifier.to(device)
        return self

class CustomDataset(torch.utils.data.Dataset):
    def __init__(self, encodings, labels):
        self.encodings = encodings
        self.labels = labels

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx])
        return item

    def __len__(self):
        return len(self.labels)

def create_dataset(dataset_name, tokenizer, split="train", max_length=64, use_subset=True):
    """Create a properly formatted dataset."""
    # Load dataset
    if use_subset:
        dataset = load_dataset(dataset_name, split=f"{split}[:1%]")
    else:
        dataset = load_dataset(dataset_name, split=split)
    
    # Tokenize texts
    encodings = tokenizer(
        dataset["text"],
        padding='max_length',
        truncation=True,
        max_length=max_length,
        return_tensors=None
    )
    
    # Get labels
    labels = dataset["label"]
    
    # Create custom dataset
    return CustomDataset(encodings, labels)

class OptimizedDistillationTrainer:
    def __init__(
        self,
        teacher_model,
        student_model,
        train_dataloader,
        eval_dataloader,
        num_epochs,
        device,
        learning_rate=1e-4,
        weight_decay=0.01,
        gradient_accumulation_steps=1
    ):
        self.teacher_model = teacher_model.to(device)
        self.student_model = student_model.to(device)
        self.train_dataloader = train_dataloader
        self.eval_dataloader = eval_dataloader
        self.num_epochs = num_epochs
        self.device = device
        self.gradient_accumulation_steps = gradient_accumulation_steps
        
        # Optimizer
        self.optimizer = torch.optim.AdamW(
            student_model.parameters(),
            lr=learning_rate,
            weight_decay=weight_decay
        )
        
        # Learning rate scheduler
        num_training_steps = len(train_dataloader) * num_epochs
        self.scheduler = get_scheduler(
            "cosine",
            optimizer=self.optimizer,
            num_warmup_steps=num_training_steps // 10,
            num_training_steps=num_training_steps
        )

    def compute_loss(self, student_logits, teacher_logits, labels, temperature=2.0, alpha=0.5):
        """Compute the distillation loss."""
        # Temperature scaling
        student_scaled = student_logits / temperature
        teacher_scaled = teacher_logits / temperature
        
        # Compute soft targets once
        soft_targets = F.softmax(teacher_scaled, dim=-1)
        student_log_probs = F.log_softmax(student_scaled, dim=-1)
        
        # Compute losses
        soft_loss = -(soft_targets * student_log_probs).sum(dim=-1).mean()
        hard_loss = F.cross_entropy(student_logits, labels)
        
        return alpha * (temperature ** 2) * soft_loss + (1 - alpha) * hard_loss

    def train(self):
        best_eval_acc = 0
        for epoch in range(self.num_epochs):
            self.student_model.train()
            self.teacher_model.eval()
            
            total_loss = 0
            progress_bar = tqdm(self.train_dataloader, desc=f"Epoch {epoch+1}")
            
            for step, batch in enumerate(progress_bar):
                # Move batch to device
                batch = {k: v.to(self.device) for k, v in batch.items()}
                
                # Get teacher predictions
                with torch.no_grad():
                    teacher_outputs = self.teacher_model(**batch)
                
                # Get student predictions and loss
                student_outputs = self.student_model(**batch)
                loss = self.compute_loss(
                    student_outputs.logits,
                    teacher_outputs.logits,
                    batch['labels']
                ) / self.gradient_accumulation_steps
                
                # Backward pass
                loss.backward()
                
                # Gradient accumulation
                if (step + 1) % self.gradient_accumulation_steps == 0:
                    torch.nn.utils.clip_grad_norm_(self.student_model.parameters(), 1.0)
                    self.optimizer.step()
                    self.scheduler.step()
                    self.optimizer.zero_grad()
                
                total_loss += loss.item() * self.gradient_accumulation_steps
                progress_bar.set_postfix({'loss': total_loss / (step + 1)})
            
            # Evaluation
            eval_acc = self.evaluate()
            print(f"Epoch {epoch+1} - Eval Accuracy: {eval_acc:.4f}")
            
            # Save best model
            if eval_acc > best_eval_acc:
                best_eval_acc = eval_acc
                self.save_model()
    
    def evaluate(self):
        self.student_model.eval()
        total_correct = 0
        total_samples = 0
        label_counts = {i: 0 for i in range(self.num_labels)}  # Track predictions per class
        
        with torch.no_grad():
            for batch in self.eval_dataloader:
                batch = {k: v.to(self.device) for k, v in batch.items()}
                outputs = self.student_model(**batch)
                predictions = outputs.logits.argmax(dim=-1)
                
                # Count predictions per class
                for pred in predictions.cpu().numpy():
                    label_counts[pred] += 1
                
                total_correct += (predictions == batch['labels']).sum().item()
                total_samples += batch['labels'].size(0)
        
        accuracy = total_correct / total_samples
        
        # Print distribution of predictions
        print("\nPrediction distribution:")
        for label_id, count in label_counts.items():
            percentage = (count / total_samples) * 100
            emotion = EMOTION_LABELS[label_id]
            print(f"{emotion}: {count} predictions ({percentage:.2f}%)")
        
        return accuracy
    
    def save_model(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        torch.save(self.student_model.state_dict(), os.path.join(SAVE_DIR, 'best_model.pth'))

class TeacherTrainer:
    def __init__(
        self,
        model,
        train_dataloader,
        eval_dataloader,
        num_epochs,
        device,
        learning_rate=5e-5,  # Lower learning rate for fine-tuning
        weight_decay=0.01
    ):
        self.model = model.to(device)
        self.train_dataloader = train_dataloader
        self.eval_dataloader = eval_dataloader
        self.num_epochs = num_epochs
        self.device = device
        
        # Separate parameter groups for fine-tuning
        # Higher learning rate for new layers, lower for pre-trained layers
        classifier_params = list(model.classifier.parameters()) + list(model.projection.parameters())
        pretrained_params = list(model.mamba.parameters())[-2:]  # Last two layers of base model
        
        self.optimizer = torch.optim.AdamW([
            {'params': classifier_params, 'lr': learning_rate},
            {'params': pretrained_params, 'lr': learning_rate * 0.1}  # Lower learning rate for pre-trained layers
        ], weight_decay=weight_decay)
        
        # Learning rate scheduler with warm-up
        num_training_steps = len(train_dataloader) * num_epochs
        num_warmup_steps = num_training_steps // 10
        
        self.scheduler = get_scheduler(
            "cosine",
            optimizer=self.optimizer,
            num_warmup_steps=num_warmup_steps,
            num_training_steps=num_training_steps
        )

    def train(self):
        best_eval_acc = 0
        early_stopping_patience = 3
        early_stopping_counter = 0
        
        for epoch in range(self.num_epochs):
            self.model.train()
            total_loss = 0
            progress_bar = tqdm(self.train_dataloader, desc=f"Teacher Fine-tuning Epoch {epoch+1}")
            
            for step, batch in enumerate(progress_bar):
                batch = {k: v.to(self.device) for k, v in batch.items()}
                
                outputs = self.model(**batch)
                loss = outputs.loss
                
                loss.backward()
                
                # Gradient clipping
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
                
                self.optimizer.step()
                self.scheduler.step()
                self.optimizer.zero_grad()
                
                total_loss += loss.item()
                progress_bar.set_postfix({
                    'loss': total_loss / (step + 1),
                    'lr': self.scheduler.get_last_lr()[0]
                })
            
            # Evaluation
            eval_acc = self.evaluate()
            print(f"Teacher Epoch {epoch+1} - Eval Accuracy: {eval_acc:.4f}")
            
            # Early stopping check
            if eval_acc > best_eval_acc:
                best_eval_acc = eval_acc
                early_stopping_counter = 0
                self.save_model()
            else:
                early_stopping_counter += 1
            
            if early_stopping_counter >= early_stopping_patience:
                print("Early stopping triggered!")
                break
        
        print(f"Best Teacher Accuracy: {best_eval_acc:.4f}")
        return best_eval_acc
    
    def evaluate(self):
        self.model.eval()
        total_correct = 0
        total_samples = 0
        
        with torch.no_grad():
            for batch in self.eval_dataloader:
                batch = {k: v.to(self.device) for k, v in batch.items()}
                outputs = self.model(**batch)
                predictions = outputs.logits.argmax(dim=-1)

                total_correct += (predictions == batch['labels']).sum().item()
                total_samples += batch['labels'].size(0)
        return total_correct / total_samples
    
    def save_model(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        torch.save(self.model.state_dict(), os.path.join(SAVE_DIR, 'best_teacher.pth'))

def main():
    # Load tokenizer
    print("Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id

    # Create datasets
    print("Creating datasets...")
    train_dataset = create_dataset(DATASET_NAME, tokenizer, split="train")
    eval_dataset = create_dataset(DATASET_NAME, tokenizer, split="test")

    # Create dataloaders
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=8,
        shuffle=True,
        num_workers=0
    )
    
    eval_dataloader = torch.utils.data.DataLoader(
        eval_dataset,
        batch_size=8,
        shuffle=False,
        num_workers=0
    )

    # Create teacher model for fine-tuning
    print("Creating teacher model...")
    base_teacher = AutoModelForCausalLM.from_pretrained(MODEL_ID)
    teacher_model = MambaForSequenceClassification(base_teacher, NUM_LABELS)
    
    # Fine-tune teacher model
    print("\nFine-tuning teacher model...")
    teacher_trainer = TeacherTrainer(
        model=teacher_model,
        train_dataloader=train_dataloader,
        eval_dataloader=eval_dataloader,
        num_epochs=10,  # More epochs for fine-tuning
        device=device
    )
    
    best_teacher_acc = teacher_trainer.train()
    
    best_teacher_acc = teacher_trainer.train()
    print(f"\nTeacher training completed. Best accuracy: {best_teacher_acc:.4f}")
    
    # Load best teacher model
    teacher_model.load_state_dict(torch.load(os.path.join(SAVE_DIR, 'best_teacher.pth')))
    teacher_model.eval()  # Set to evaluation mode
    
    # Create student model
    print("\nCreating student model...")
    student_config = AutoConfig.from_pretrained(MODEL_ID)
    student_config.d_model = student_config.d_model // 2
    student_config.n_layer = max(1, student_config.n_layer // 4)
    base_student = AutoModelForCausalLM.from_config(student_config)
    student_model = MambaForSequenceClassification(base_student, NUM_LABELS)
    student_model = student_model.to(device)
    
    print("\nTeacher model dimensions:")
    print(f"Hidden size: {teacher_model.hidden_size}")
    print("\nStudent model dimensions:")
    print(f"Hidden size: {student_model.hidden_size}")

    # Initialize distillation trainer
    print("\nStarting distillation...")
    distillation_trainer = OptimizedDistillationTrainer(
        teacher_model=teacher_model,
        student_model=student_model,
        train_dataloader=train_dataloader,
        eval_dataloader=eval_dataloader,
        num_epochs=3,
        device=device,
        learning_rate=1e-4,
        gradient_accumulation_steps=2
    )

    # Train student through distillation
    distillation_trainer.train()

    # Test examples with emotion labels
    test_texts = [
        "I feel happy today!",
        "This makes me so angry!",
        "I'm really sad about what happened.",
        "What an amazing surprise!",
        "I love you so much!",
        "This is terrifying!"
    ]
    
    print("\nTesting both models:")
    for text in test_texts:
        print(f"\nText: {text}")
        
        # Teacher prediction
        teacher_pred, teacher_probs = predict_text(text, teacher_model, tokenizer, device)
        teacher_emotion = EMOTION_LABELS[teacher_pred]
        print(f"Teacher prediction: {teacher_emotion} (confidence: {teacher_probs[teacher_pred]:.4f})")
        print("Teacher probabilities for each emotion:")
        for i, prob in enumerate(teacher_probs):
            print(f"  {EMOTION_LABELS[i]}: {prob:.4f}")
        
        # Student prediction
        student_pred, student_probs = predict_text(text, student_model, tokenizer, device)
        student_emotion = EMOTION_LABELS[student_pred]
        print(f"Student prediction: {student_emotion} (confidence: {student_probs[student_pred]:.4f})")
        print("Student probabilities for each emotion:")
        for i, prob in enumerate(student_probs):
            print(f"  {EMOTION_LABELS[i]}: {prob:.4f}")

    # Print overall statistics
    print("\nOverall Prediction Statistics:")
    print("\nEvaluating Teacher Model...")
    teacher_model.eval()
    label_counts = {i: 0 for i in range(NUM_LABELS)}
    total_samples = 0
    
    with torch.no_grad():
        for batch in eval_dataloader:
            batch = {k: v.to(device) for k, v in batch.items()}
            outputs = teacher_model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            
            for pred in predictions.cpu().numpy():
                label_counts[pred] += 1
            total_samples += predictions.size(0)
    
    print("\nTeacher Model Distribution:")
    for label_id, count in label_counts.items():
        percentage = (count / total_samples) * 100
        emotion = EMOTION_LABELS[label_id]
        print(f"{emotion}: {count} predictions ({percentage:.2f}%)")
    
    print("\nEvaluating Student Model...")
    student_model.eval()
    label_counts = {i: 0 for i in range(NUM_LABELS)}
    
    with torch.no_grad():
        for batch in eval_dataloader:
            batch = {k: v.to(device) for k, v in batch.items()}
            outputs = student_model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            
            for pred in predictions.cpu().numpy():
                label_counts[pred] += 1
    
    print("\nStudent Model Distribution:")
    for label_id, count in label_counts.items():
        percentage = (count / total_samples) * 100
        emotion = EMOTION_LABELS[label_id]
        print(f"{emotion}: {count} predictions ({percentage:.2f}%)")

def predict_text(text, model, tokenizer, device):
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    
    with torch.no_grad():
        outputs = model(**inputs)
        probs = F.softmax(outputs.logits, dim=-1)
        prediction = torch.argmax(probs, dim=-1)
    
    return prediction.item(), probs[0].cpu().numpy()

if __name__ == "__main__":
    main()