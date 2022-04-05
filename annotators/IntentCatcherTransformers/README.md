## IntentCatcher based on Transformers


English version was trained on `intent_phrases.json` dataset via command:
```
python create_data_and_train_model.py --intent_phrases_path intent_phrases.json --model_path ./data/intents_model_v0 --epochs 3
```
It consumes 2 Gb GPU RAM during fine-tuning `distilbert-base-uncased`. Classification results are the following:
```json
{
    'eval_loss': 0.00016819244774524122, 
    'eval_f1': 0.9989117214006786, 
    'eval_roc_auc': 0.9995375562004283, 
    'eval_accuracy': 0.9991570493662544, 
    'eval_runtime': 215.068, 
    'eval_samples_per_second': 606.757, 
    'epoch': 3.0
}
```

