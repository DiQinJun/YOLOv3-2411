# import sagemaker
# from sagemaker.pytorch import PyTorch

# sagemaker_session = sagemaker.Session()
# role = sagemaker.get_execution_role()

# estimator = PyTorch(entry_point='train.py',
#                     source_dir='./',
#                     role=role,
#                     framework_version='1.2.0',
#                     train_instance_count=1,
#                     train_instance_type='ml.t2.medium',
#                     hyperparameters={'epochs': 1},
#                    )

# # Train the network...
# estimator.fit({
#     'train': 's3://sagemaker-smallset/dataset',
#     'config': 's3://sagemaker-smallset/cfg'
# })
